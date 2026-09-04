"""Background quota polling and persisted notification delivery."""

import asyncio
import contextlib
import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from nonebot import logger

from .alerts import AlertEvent, evaluate_accounts, event_key
from .formatting import format_alert_batch
from .models import CodexAccountQuota
from .portal import (
    PortalClient,
    PortalProtocolError,
    PortalUnauthorizedError,
    PortalUnavailableError,
    PortalUpstreamError,
)
from .quota import extract_codex_weekly_accounts
from .storage import PendingNotification, PersistedState, StateStore

_POLL_INTERVAL_SECONDS = 60
_SEND_TIMEOUT_SECONDS = 30
_STOP_GRACE_SECONDS = 35
_BASE_RETRY_SECONDS = 60
_MAX_RETRY_SECONDS = 60 * 60

SendNotification = Callable[[int, str], Awaitable[None]]


class MonitorNotInitializedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Mzk1 AI quota monitor is not initialized")


class QuotaMonitor:
    """Single sequential worker for quota transitions and delivery."""

    def __init__(
        self,
        portal: PortalClient,
        store: StateStore,
        group_id: int,
        thresholds: tuple[int, ...],
        send_notification: SendNotification,
    ) -> None:
        self._portal = portal
        self._store = store
        self._group_id = group_id
        self._thresholds = thresholds
        self._send_notification = send_notification
        self._state: PersistedState | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._last_poll_error: str | None = None
        self._invalid_account_count = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._state = await self._store.load()
        await self._discard_stale_notifications()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="mzk1-ai-quota-monitor")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        self._wake_event.set()
        done, _ = await asyncio.wait({task}, timeout=_STOP_GRACE_SECONDS)
        if task in done:
            await task
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None

    def wake(self) -> None:
        self._wake_event.set()

    def weekly_window_activity(self) -> dict[str, bool | None]:
        state = self._state
        if state is None:
            return {}
        return {
            credential_id: credential.weekly.window_active
            for credential_id, credential in state.credentials.items()
            if credential.weekly is not None
        }

    async def run_once(self) -> None:
        """Run one poll and delivery pass."""
        if self._state is None:
            self._state = await self._store.load()
            await self._discard_stale_notifications()
        await self._poll_quota()
        await self._deliver_pending()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("Mzk1 AI quota monitor iteration failed")

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=_POLL_INTERVAL_SECONDS,
                )

    async def _poll_quota(self) -> None:
        state = self._require_state()
        try:
            snapshot = await self._portal.quota()
            accounts = extract_codex_weekly_accounts(snapshot)
            self._log_invalid_accounts(accounts)
        except PortalUnauthorizedError as error:
            self._log_poll_error("unauthorized", error, error_level=True)
            return
        except PortalProtocolError as error:
            self._log_poll_error("protocol", error)
            return
        except (PortalUpstreamError, PortalUnavailableError) as error:
            self._log_poll_error("unavailable", error)
            return

        if self._last_poll_error is not None:
            logger.info("Mzk1 AI quota polling recovered")
            self._last_poll_error = None
        evaluation = evaluate_accounts(state, accounts, self._thresholds)
        if evaluation.events:
            self._enqueue_events(list(evaluation.events))
        if evaluation.changed or evaluation.events:
            await self._store.save(state)

    async def _discard_stale_notifications(self) -> None:
        state = self._require_state()
        current = [
            item
            for item in state.pending_notifications
            if item.group_id == self._group_id
        ]
        discarded = len(state.pending_notifications) - len(current)
        if discarded == 0:
            return
        state.pending_notifications = current
        await self._store.save(state)
        logger.warning(
            "Mzk1 AI discarded {} notification(s) for an old target group",
            discarded,
        )

    async def _deliver_pending(self) -> None:
        state = self._require_state()
        now = datetime.now(timezone.utc)
        due = [
            notification
            for notification in state.pending_notifications
            if notification.next_attempt_at <= now
        ]
        for notification in due:
            try:
                await asyncio.wait_for(
                    self._send_notification(
                        notification.group_id,
                        notification.message,
                    ),
                    timeout=_SEND_TIMEOUT_SECONDS,
                )
            except Exception as error:  # noqa: BLE001
                notification.attempts += 1
                exponent = max(notification.attempts - 1, 0)
                delay = min(
                    _MAX_RETRY_SECONDS,
                    _BASE_RETRY_SECONDS * 2 ** min(exponent, 6),
                )
                notification.next_attempt_at = now + timedelta(seconds=delay)
                logger.warning(
                    "Mzk1 AI notification delivery failed ({}): {}",
                    notification.attempts,
                    type(error).__name__,
                )
            else:
                state.pending_notifications.remove(notification)
            await self._store.save(state)

    def _log_poll_error(
        self,
        key: str,
        error: Exception,
        *,
        error_level: bool = False,
    ) -> None:
        if self._last_poll_error == key:
            return
        self._last_poll_error = key
        message = "Mzk1 AI quota polling failed: {}"
        if error_level:
            logger.error(message, type(error).__name__)
        else:
            logger.warning(message, type(error).__name__)

    def _log_invalid_accounts(self, accounts: list[CodexAccountQuota]) -> None:
        invalid_count = sum(account.status == "invalid_weekly" for account in accounts)
        if invalid_count == self._invalid_account_count:
            return
        self._invalid_account_count = invalid_count
        if invalid_count:
            logger.warning(
                "Mzk1 AI found {} account(s) with incompatible Weekly quota data",
                invalid_count,
            )
        else:
            logger.info("Mzk1 AI account-level Weekly quota data recovered")

    def _enqueue_events(self, events: list[AlertEvent]) -> None:
        keys = sorted(event_key(event) for event in events)
        digest = hashlib.sha256("\n".join(keys).encode()).hexdigest()
        self._enqueue_message(f"alert-batch:{digest}", format_alert_batch(events))

    def _enqueue_message(self, key: str, message: str) -> None:
        state = self._require_state()
        if any(item.event_key == key for item in state.pending_notifications):
            return
        now = datetime.now(timezone.utc)
        state.pending_notifications.append(
            PendingNotification(
                event_key=key,
                group_id=self._group_id,
                message=message,
                created_at=now,
                next_attempt_at=now,
            )
        )

    def _require_state(self) -> PersistedState:
        if self._state is None:
            raise MonitorNotInitializedError
        return self._state
