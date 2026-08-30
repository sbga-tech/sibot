"""State transitions for Codex Weekly quota and login alerts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypeAlias

from .models import CodexAccountQuota, CodexWeeklyQuota
from .storage import CredentialAlertState, PersistedState, WeeklyAlertState

_RESET_TOLERANCE = timedelta(minutes=2)
_WINDOW_ACTIVITY_SAMPLE_INTERVAL = timedelta(minutes=4)
_FULL_PERCENT = 100.0
_EXHAUSTED_THRESHOLD = 0
_HTTP_UNAUTHORIZED = 401

ResetSource: TypeAlias = Literal["scheduled", "reset_credit", "openai"]


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
    source: ResetSource
    remaining_percent: float
    reset_at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ResetCreditIncreasedAlert:
    kind: Literal["reset_credit_increased"]
    credential_id: str
    display_name: str
    added_count: int
    available_count: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class LoginInvalidAlert:
    kind: Literal["login_invalid"]
    credential_id: str
    display_name: str
    observed_at: datetime


AlertEvent: TypeAlias = (
    WeeklyLowAlert
    | WeeklyExhaustedAlert
    | WeeklyResetAlert
    | ResetCreditIncreasedAlert
    | LoginInvalidAlert
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
        return (
            f"weekly-reset:{event.source}:{event.credential_id}:"
            f"{event.observed_at.isoformat()}"
        )
    if isinstance(event, ResetCreditIncreasedAlert):
        return (
            f"reset-credit-increased:{event.credential_id}:"
            f"{event.observed_at.isoformat()}"
        )
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
    previous_credits = credential.reset_credits_available
    current_credits = account.reset_credits_available

    if account.weekly is not None:
        event = _evaluate_weekly_quota(
            credential,
            account,
            thresholds,
            previous_credits,
            current_credits,
        )
        if event is not None:
            events.append(event)
    credit_event = _reset_credit_increase_event(
        account,
        observed_at,
        previous_credits,
        current_credits,
    )
    if credit_event is not None:
        events.append(credit_event)
    if current_credits is not None:
        credential.reset_credits_available = current_credits


def _evaluate_weekly_quota(
    credential: CredentialAlertState,
    account: CodexAccountQuota,
    thresholds: tuple[int, ...],
    previous_credits: int | None,
    current_credits: int | None,
) -> AlertEvent | None:
    weekly = account.weekly
    observed_at = account.refreshed_at
    if weekly is None or observed_at is None:
        return None
    if credential.weekly is None:
        credential.weekly = _new_weekly_baseline(
            weekly,
            observed_at,
            thresholds,
            window_active=_initial_window_activity(weekly),
        )
        return None

    previous = credential.weekly
    reset_source = _detect_reset_source(
        previous,
        weekly,
        observed_at,
        previous_credits,
        current_credits,
    )
    if reset_source is not None:
        credential.weekly = _new_weekly_baseline(
            weekly,
            observed_at,
            thresholds,
            window_active=_is_directly_active(weekly),
        )
        return WeeklyResetAlert(
            kind="weekly_reset",
            credential_id=account.credential_id,
            display_name=account.display_name,
            source=reset_source,
            remaining_percent=_effective_remaining(weekly),
            reset_at=weekly.reset_at,
            observed_at=observed_at,
        )

    window_active = _detect_window_activity(previous, weekly, observed_at)
    event = _threshold_event(previous, account, thresholds)
    previous.remaining_percent = _effective_remaining(weekly)
    previous.reset_at = weekly.reset_at
    previous.window_seconds = weekly.window_seconds
    previous.last_observed_at = observed_at
    previous.window_active = window_active
    return event


def _reset_credit_increase_event(
    account: CodexAccountQuota,
    observed_at: datetime,
    previous_credits: int | None,
    current_credits: int | None,
) -> ResetCreditIncreasedAlert | None:
    if (
        previous_credits is None
        or current_credits is None
        or current_credits <= previous_credits
    ):
        return None
    return ResetCreditIncreasedAlert(
        kind="reset_credit_increased",
        credential_id=account.credential_id,
        display_name=account.display_name,
        added_count=current_credits - previous_credits,
        available_count=current_credits,
        observed_at=observed_at,
    )


def _threshold_event(
    previous: WeeklyAlertState,
    account: CodexAccountQuota,
    thresholds: tuple[int, ...],
) -> AlertEvent | None:
    weekly = account.weekly
    observed_at = account.refreshed_at
    if weekly is None or observed_at is None:
        return None
    current_remaining = _effective_remaining(weekly)
    crossed = tuple(
        threshold
        for threshold in thresholds
        if threshold not in previous.notified_thresholds
        and previous.remaining_percent > threshold
        and current_remaining <= threshold
    )
    if not crossed:
        return None
    previous.notified_thresholds.update(crossed)
    if weekly.exhausted or _EXHAUSTED_THRESHOLD in crossed:
        return WeeklyExhaustedAlert(
            kind="weekly_exhausted",
            credential_id=account.credential_id,
            display_name=account.display_name,
            reset_at=weekly.reset_at,
            observed_at=observed_at,
        )
    return WeeklyLowAlert(
        kind="weekly_low",
        credential_id=account.credential_id,
        display_name=account.display_name,
        remaining_percent=current_remaining,
        thresholds=crossed,
        reset_at=weekly.reset_at,
        observed_at=observed_at,
    )


def _new_weekly_baseline(
    weekly: CodexWeeklyQuota,
    observed_at: datetime,
    thresholds: tuple[int, ...],
    *,
    window_active: bool | None,
) -> WeeklyAlertState:
    remaining = _effective_remaining(weekly)
    return WeeklyAlertState(
        remaining_percent=remaining,
        reset_at=weekly.reset_at,
        window_seconds=weekly.window_seconds,
        notified_thresholds={
            threshold for threshold in thresholds if remaining <= threshold
        },
        last_observed_at=observed_at,
        window_active=window_active,
    )


def _effective_remaining(weekly: CodexWeeklyQuota) -> float:
    return 0.0 if weekly.exhausted else weekly.remaining_percent


def _initial_window_activity(weekly: CodexWeeklyQuota) -> bool | None:
    if _is_directly_active(weekly):
        return True
    return None


def _is_directly_active(weekly: CodexWeeklyQuota) -> bool:
    return weekly.exhausted or _effective_remaining(weekly) < _FULL_PERCENT


def _detect_window_activity(
    previous: WeeklyAlertState,
    current: CodexWeeklyQuota,
    observed_at: datetime,
) -> bool | None:
    if _is_directly_active(current):
        return True
    if previous.last_observed_at is None:
        return previous.window_active

    observed_shift = observed_at - previous.last_observed_at
    if observed_shift <= _WINDOW_ACTIVITY_SAMPLE_INTERVAL:
        return previous.window_active

    reset_shift = current.reset_at - previous.reset_at
    if abs(reset_shift) <= _RESET_TOLERANCE:
        return True
    if abs(reset_shift - observed_shift) <= _RESET_TOLERANCE:
        return False
    return previous.window_active


def _same_cycle(
    previous: WeeklyAlertState,
    current: CodexWeeklyQuota,
) -> bool:
    if previous.window_seconds != current.window_seconds:
        return False
    return abs(current.reset_at - previous.reset_at) <= _RESET_TOLERANCE


def _detect_reset_source(
    previous: WeeklyAlertState,
    current: CodexWeeklyQuota,
    observed_at: datetime,
    previous_credits: int | None,
    current_credits: int | None,
) -> ResetSource | None:
    cycle_changed = not _same_cycle(previous, current)
    remaining_increased = _effective_remaining(current) > previous.remaining_percent
    active_cycle_changed = previous.window_active is True and cycle_changed
    if not remaining_increased and not active_cycle_changed:
        return None

    if (
        previous_credits is not None
        and current_credits is not None
        and current_credits < previous_credits
    ):
        return "reset_credit"
    if (
        previous.window_active is True
        and observed_at >= previous.reset_at - _RESET_TOLERANCE
    ):
        return "scheduled"
    return "openai"
