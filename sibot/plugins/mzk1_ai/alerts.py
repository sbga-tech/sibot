"""State transitions for Codex Weekly quota and login alerts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypeAlias

from .models import CodexAccountQuota, CodexWeeklyQuota
from .storage import (
    CredentialAlertState,
    PersistedState,
    WeeklyAlertState,
)

_RESET_TOLERANCE = timedelta(minutes=2)
_EXHAUSTED_THRESHOLD = 0
_HTTP_UNAUTHORIZED = 401


@dataclass(frozen=True, slots=True)
class WeeklyLowAlert:
    kind: Literal["weekly_low"]
    credential_id: str
    display_name: str
    remaining_percent: float
    thresholds: tuple[int, ...]
    reset_at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class WeeklyExhaustedAlert:
    kind: Literal["weekly_exhausted"]
    credential_id: str
    display_name: str
    reset_at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class WeeklyResetAlert:
    kind: Literal["weekly_reset"]
    credential_id: str
    display_name: str
    remaining_percent: float
    reset_at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class LoginInvalidAlert:
    kind: Literal["login_invalid"]
    credential_id: str
    display_name: str
    observed_at: datetime


AlertEvent: TypeAlias = (
    WeeklyLowAlert | WeeklyExhaustedAlert | WeeklyResetAlert | LoginInvalidAlert
)


@dataclass(frozen=True, slots=True)
class AlertEvaluation:
    events: tuple[AlertEvent, ...]
    changed: bool


def evaluate_accounts(
    state: PersistedState,
    accounts: list[CodexAccountQuota],
    thresholds: tuple[int, ...],
) -> AlertEvaluation:
    """Apply new cache observations to persisted state."""
    events: list[AlertEvent] = []
    changed = False
    for account in accounts:
        changed |= _evaluate_account(state, account, thresholds, events)
    return AlertEvaluation(events=tuple(events), changed=changed)


def event_key(event: AlertEvent) -> str:
    """Return a stable outbox key for one state transition."""
    if isinstance(event, WeeklyLowAlert):
        threshold = min(event.thresholds)
        return (
            f"weekly-low:{event.credential_id}:{event.reset_at.isoformat()}:{threshold}"
        )
    if isinstance(event, WeeklyExhaustedAlert):
        return f"weekly-exhausted:{event.credential_id}:{event.reset_at.isoformat()}"
    if isinstance(event, WeeklyResetAlert):
        return f"weekly-reset:{event.credential_id}:{event.reset_at.isoformat()}"
    return f"{event.kind}:{event.credential_id}:{event.observed_at.isoformat()}"


def _evaluate_account(
    state: PersistedState,
    account: CodexAccountQuota,
    thresholds: tuple[int, ...],
    events: list[AlertEvent],
) -> bool:
    observed_at = account.refreshed_at
    if account.status == "missing" or observed_at is None:
        return False

    credential = state.credentials.get(account.credential_id)
    if credential is None:
        credential = CredentialAlertState()
        state.credentials[account.credential_id] = credential
    if _already_observed(credential, observed_at):
        return False

    credential.last_refreshed_at = observed_at
    if account.status == "failed":
        _evaluate_failed_account(credential, account, observed_at, events)
        return True

    _evaluate_successful_account(
        credential,
        account,
        observed_at,
        thresholds,
        events,
    )
    return True


def _already_observed(
    credential: CredentialAlertState,
    observed_at: datetime,
) -> bool:
    return (
        credential.last_refreshed_at is not None
        and observed_at <= credential.last_refreshed_at
    )


def _evaluate_failed_account(
    credential: CredentialAlertState,
    account: CodexAccountQuota,
    observed_at: datetime,
    events: list[AlertEvent],
) -> None:
    if account.http_status_code != _HTTP_UNAUTHORIZED:
        return
    if credential.auth_status != "invalid":
        events.append(
            LoginInvalidAlert(
                kind="login_invalid",
                credential_id=account.credential_id,
                display_name=account.display_name,
                observed_at=observed_at,
            )
        )
    credential.auth_status = "invalid"


def _evaluate_successful_account(
    credential: CredentialAlertState,
    account: CodexAccountQuota,
    observed_at: datetime,
    thresholds: tuple[int, ...],
    events: list[AlertEvent],
) -> None:
    credential.auth_status = "normal"
    if account.weekly is None:
        return
    _evaluate_weekly_quota(
        credential,
        account,
        observed_at,
        thresholds,
        events,
    )


def _evaluate_weekly_quota(
    credential: CredentialAlertState,
    account: CodexAccountQuota,
    observed_at: datetime,
    thresholds: tuple[int, ...],
    events: list[AlertEvent],
) -> None:
    weekly = account.weekly
    if weekly is None:
        return
    if credential.weekly is None:
        credential.weekly = _new_weekly_baseline(weekly, thresholds)
        return

    previous = credential.weekly
    if _is_stale_cycle(previous, weekly):
        return
    if _is_new_cycle(previous, weekly, observed_at):
        events.append(_new_cycle_event(account, weekly, observed_at, thresholds))
        credential.weekly = _new_weekly_baseline(weekly, thresholds)
        return

    _evaluate_thresholds(previous, account, thresholds, events)
    previous.remaining_percent = _effective_remaining(weekly)
    previous.reset_at = weekly.reset_at
    previous.window_seconds = weekly.window_seconds


def _evaluate_thresholds(
    previous: WeeklyAlertState,
    account: CodexAccountQuota,
    thresholds: tuple[int, ...],
    events: list[AlertEvent],
) -> None:
    weekly = account.weekly
    observed_at = account.refreshed_at
    if weekly is None or observed_at is None:
        return
    current_remaining = _effective_remaining(weekly)
    crossed = tuple(
        threshold
        for threshold in thresholds
        if threshold not in previous.notified_thresholds
        and previous.remaining_percent > threshold
        and current_remaining <= threshold
    )
    if not crossed:
        return
    previous.notified_thresholds.update(crossed)
    if weekly.exhausted or _EXHAUSTED_THRESHOLD in crossed:
        events.append(
            WeeklyExhaustedAlert(
                kind="weekly_exhausted",
                credential_id=account.credential_id,
                display_name=account.display_name,
                reset_at=weekly.reset_at,
                observed_at=observed_at,
            )
        )
        return
    events.append(
        WeeklyLowAlert(
            kind="weekly_low",
            credential_id=account.credential_id,
            display_name=account.display_name,
            remaining_percent=current_remaining,
            thresholds=crossed,
            reset_at=weekly.reset_at,
            observed_at=observed_at,
        )
    )


def _new_cycle_event(
    account: CodexAccountQuota,
    weekly: CodexWeeklyQuota,
    observed_at: datetime,
    thresholds: tuple[int, ...],
) -> AlertEvent:
    remaining = _effective_remaining(weekly)
    if weekly.exhausted:
        return WeeklyExhaustedAlert(
            kind="weekly_exhausted",
            credential_id=account.credential_id,
            display_name=account.display_name,
            reset_at=weekly.reset_at,
            observed_at=observed_at,
        )
    crossed = tuple(threshold for threshold in thresholds if remaining <= threshold)
    if crossed:
        return WeeklyLowAlert(
            kind="weekly_low",
            credential_id=account.credential_id,
            display_name=account.display_name,
            remaining_percent=remaining,
            thresholds=crossed,
            reset_at=weekly.reset_at,
            observed_at=observed_at,
        )
    return WeeklyResetAlert(
        kind="weekly_reset",
        credential_id=account.credential_id,
        display_name=account.display_name,
        remaining_percent=remaining,
        reset_at=weekly.reset_at,
        observed_at=observed_at,
    )


def _new_weekly_baseline(
    weekly: CodexWeeklyQuota,
    thresholds: tuple[int, ...],
) -> WeeklyAlertState:
    remaining = _effective_remaining(weekly)
    return WeeklyAlertState(
        remaining_percent=remaining,
        reset_at=weekly.reset_at,
        window_seconds=weekly.window_seconds,
        notified_thresholds={
            threshold for threshold in thresholds if remaining <= threshold
        },
    )


def _effective_remaining(weekly: CodexWeeklyQuota) -> float:
    return 0.0 if weekly.exhausted else weekly.remaining_percent


def _same_cycle(
    previous: WeeklyAlertState,
    current: CodexWeeklyQuota,
) -> bool:
    if previous.window_seconds != current.window_seconds:
        return False
    return abs(current.reset_at - previous.reset_at) <= _RESET_TOLERANCE


def _is_new_cycle(
    previous: WeeklyAlertState,
    current: CodexWeeklyQuota,
    observed_at: datetime,
) -> bool:
    if _same_cycle(previous, current):
        return False
    if current.window_seconds != previous.window_seconds:
        return observed_at >= previous.reset_at - _RESET_TOLERANCE
    return (
        current.reset_at > previous.reset_at
        and observed_at >= previous.reset_at - _RESET_TOLERANCE
    )


def _is_stale_cycle(
    previous: WeeklyAlertState,
    current: CodexWeeklyQuota,
) -> bool:
    return not _same_cycle(previous, current) and current.reset_at < previous.reset_at
