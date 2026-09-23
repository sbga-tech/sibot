"""On-demand forecasts from observed, reset-separated pool quota consumption."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from nonebot import logger

from .models import (
    CodexAccountQuota,
    ForecastProblem,
    ForecastScenario,
    ForecastWarning,
    PoolForecast,
    QuotaHistoryCycle,
    QuotaHistoryResponse,
    QuotaRoutingCredential,
    QuotaRoutingSnapshot,
)
from .portal import PortalClient, PortalError
from .quota import WEEKLY_WINDOW_SECONDS

_HISTORY_QUERY_CONCURRENCY = 4
_LOOKBACK_HOURS = (24, 6)
_MAX_SAMPLE_AGE = timedelta(minutes=30)
_CYCLE_TIME_TOLERANCE = timedelta(minutes=2)
_ROUTING_TIME_PRECISION = timedelta(seconds=1)
_FULL_PERCENT = 100.0
# One account's full Plus weekly quota is 100 pool points.
_PLAN_CAPACITIES = {"plus": 1, "pro-5x": 5, "pro-20x": 20}


async def load_pool_forecast(
    portal: PortalClient,
    accounts: Sequence[CodexAccountQuota],
    window_activity: Mapping[str, bool | None],
) -> PoolForecast:
    try:
        routing = await portal.quota_routing()
    except PortalError:
        return PoolForecast(
            generated_at=datetime.now(timezone.utc), problem="missing_routing"
        )
    semaphore = asyncio.Semaphore(_HISTORY_QUERY_CONCURRENCY)
    histories = await asyncio.gather(
        *(_load_history(portal, account, semaphore) for account in accounts)
    )
    return forecast_pool(
        accounts,
        routing,
        dict(
            zip((account.credential_id for account in accounts), histories, strict=True)
        ),
        datetime.now(timezone.utc),
        window_activity,
    )


async def _load_history(
    portal: PortalClient,
    account: CodexAccountQuota,
    semaphore: asyncio.Semaphore,
) -> QuotaHistoryResponse | None:
    if account.weekly is None:
        return None
    async with semaphore:
        try:
            return await portal.quota_history(
                account.credential_id, account.weekly.window_role
            )
        except PortalError as error:
            logger.warning(
                "Mzk1 AI quota history query failed: {}", type(error).__name__
            )
            return None


def forecast_pool(
    accounts: Sequence[CodexAccountQuota],
    routing: QuotaRoutingSnapshot,
    histories: Mapping[str, QuotaHistoryResponse | None],
    now: datetime,
    window_activity: Mapping[str, bool | None],
) -> PoolForecast:
    """Estimate the whole pool only up to its next known natural replenishment."""
    problem = _quota_problem(accounts, now)
    if problem is not None:
        return PoolForecast(generated_at=now, problem=problem)
    forecast = _pool_snapshot(accounts, routing, now, window_activity)
    if forecast.problem is not None:
        return forecast

    weekly_histories = [
        _weekly_history(account, histories.get(account.credential_id), now)
        for account in accounts
    ]
    if any(history is None for history in weekly_histories):
        return replace(forecast, problem="missing_history")
    complete_histories = [
        history for history in weekly_histories if history is not None
    ]
    scenarios = tuple(
        scenario
        for hours in _LOOKBACK_HOURS
        if (
            scenario := _scenario(
                forecast, complete_histories, accounts, routing, hours
            )
        )
        is not None
    )
    if not scenarios:
        return replace(forecast, problem="missing_history")
    observed_at = min(
        max(cycle.last_observed_at for cycle in history)
        for history in complete_histories
    )
    if forecast.observed_at is not None:
        observed_at = min(observed_at, forecast.observed_at)
    warnings = forecast.warnings
    if len(scenarios) < len(_LOOKBACK_HOURS):
        warnings += ("short_history",)
    return replace(
        forecast, scenarios=scenarios, observed_at=observed_at, warnings=warnings
    )


def _quota_problem(
    accounts: Sequence[CodexAccountQuota], now: datetime
) -> ForecastProblem | None:
    if not accounts:
        return "no_accounts"
    if any(
        account.status != "completed" or account.weekly is None for account in accounts
    ):
        return "missing_quota"
    for account in accounts:
        weekly = account.weekly
        if weekly is None:
            return "missing_quota"
        if (
            not _fresh(account.refreshed_at, now)
            or weekly.reset_at.utcoffset() is None
            or weekly.reset_at <= now
        ):
            return "stale_quota"
    if any(account.plan not in _PLAN_CAPACITIES for account in accounts):
        return "unsupported_plans"
    return None


def _capacity(account: CodexAccountQuota) -> int:
    # Only known plans pass _quota_problem; no arbitrary default multiplier.
    return _PLAN_CAPACITIES[account.plan or ""]


def _pool_snapshot(
    accounts: Sequence[CodexAccountQuota],
    routing: QuotaRoutingSnapshot,
    now: datetime,
    window_activity: Mapping[str, bool | None],
) -> PoolForecast:
    statuses = {
        credential.credential_id: credential for credential in routing.credentials
    }
    if not _fresh(routing.generated_at, now) or any(
        account.credential_id not in statuses
        or statuses[account.credential_id].provider.lower() != "codex"
        for account in accounts
    ):
        return PoolForecast(generated_at=now, problem="missing_routing")

    remaining = 0.0
    available = 0.0
    reset_times: list[datetime] = []
    for account in accounts:
        weekly = account.weekly
        assert weekly is not None  # Validated by _quota_problem.
        capacity = _capacity(account)
        remaining += weekly.remaining_percent * capacity
        routable = _routable(statuses[account.credential_id], now)
        if routable:
            available += weekly.remaining_percent * capacity
        # Cooldowns can end at reset; disabled and zero-weight accounts cannot
        # contribute then. Allow the timestamp precision difference between APIs.
        if _routable(
            statuses[account.credential_id], weekly.reset_at + _ROUTING_TIME_PRECISION
        ) and (
            weekly.remaining_percent < _FULL_PERCENT
            or window_activity.get(account.credential_id) is True
        ):
            reset_times.append(weekly.reset_at)
    next_reset = min(reset_times) if reset_times else None
    warnings: list[ForecastWarning] = []
    if available < remaining:
        warnings.append("routing_limited")
    if next_reset is not None and any(
        account.subscription_active_until is not None
        and account.subscription_active_until <= next_reset
        for account in accounts
    ):
        warnings.append("subscription_expiring")
    return PoolForecast(
        generated_at=now,
        problem="unstarted_window" if next_reset is None else None,
        remaining_plus_points=remaining,
        available_plus_points=available,
        next_reset_at=next_reset,
        observed_at=min(
            account.refreshed_at
            for account in accounts
            if account.refreshed_at is not None
        ),
        warnings=tuple(warnings),
    )


def _routable(status: QuotaRoutingCredential, now: datetime) -> bool:
    if status.disabled or (status.weight is not None and status.weight <= 0):
        return False
    return not status.unavailable or (
        status.next_retry_after is not None and status.next_retry_after <= now
    )


def _fresh(observed_at: datetime | None, now: datetime) -> bool:
    return (
        observed_at is not None
        and observed_at.utcoffset() is not None
        and -_CYCLE_TIME_TOLERANCE <= now - observed_at <= _MAX_SAMPLE_AGE
    )


def _weekly_history(
    account: CodexAccountQuota,
    history: QuotaHistoryResponse | None,
    now: datetime,
) -> list[QuotaHistoryCycle] | None:
    weekly = account.weekly
    if weekly is None or history is None or history.selected_window is None:
        return None
    if (
        history.selected_window.window_role != weekly.window_role
        or history.selected_window.window_seconds != WEEKLY_WINDOW_SECONDS
    ):
        return None
    cycles = sorted(
        (
            cycle
            for cycle in history.cycles
            if cycle.window_seconds == WEEKLY_WINDOW_SECONDS
        ),
        key=lambda cycle: cycle.first_observed_at,
    )
    if not any(
        cycle.status == "current"
        and abs(cycle.reset_at - weekly.reset_at) <= _CYCLE_TIME_TOLERANCE
        and _fresh(cycle.last_observed_at, now)
        for cycle in cycles
    ):
        return None
    return cycles


def _scenario(
    forecast: PoolForecast,
    histories: Sequence[Sequence[QuotaHistoryCycle]],
    accounts: Sequence[CodexAccountQuota],
    routing: QuotaRoutingSnapshot,
    hours: int,
) -> ForecastScenario | None:
    end = forecast.generated_at
    start = end - timedelta(hours=hours)
    burns = [_covered_burn(history, start, end) for history in histories]
    if any(burn is None for burn in burns):
        return None
    # Fixed Plus-based points: do not divide by today's pool size. An account
    # now in cooldown still contributes its observed consumption.
    rate = (
        sum(
            burn * _capacity(account)
            for account, burn in zip(accounts, burns, strict=True)
            if burn is not None
        )
        / hours
    )
    if rate <= 0:
        return ForecastScenario(hours, rate, None, None, None)
    assert forecast.available_plus_points is not None
    assert forecast.next_reset_at is not None
    refill_hours = (forecast.next_reset_at - end).total_seconds() / 3600
    statuses = {item.credential_id: item for item in routing.credentials}
    projected_balance = 0.0
    for account, burn in zip(accounts, burns, strict=True):
        if not _routable(statuses[account.credential_id], end):
            continue
        assert account.weekly is not None and account.refreshed_at is not None
        assert burn is not None
        age_hours = max(0.0, (end - account.refreshed_at).total_seconds() / 3600)
        projected_balance += max(
            0.0, account.weekly.remaining_percent - burn / hours * age_hours
        ) * _capacity(account)
    runway_hours = projected_balance / rate
    return ForecastScenario(
        lookback_hours=hours,
        burn_plus_points_per_hour=rate,
        runway_hours=runway_hours,
        reaches_reset=runway_hours >= refill_hours,
        target_fraction=min(1.0, runway_hours / refill_hours),
    )


def _covered_burn(
    cycles: Sequence[QuotaHistoryCycle], start: datetime, end: datetime
) -> float | None:
    """Interpolate boundary transitions, without charging reset jumps as usage."""
    covered_until = start
    burned = 0.0
    for cycle in cycles:
        if cycle.last_observed_at < start or cycle.first_observed_at > end:
            continue
        if cycle.first_observed_at > covered_until + _MAX_SAMPLE_AGE:
            return None
        covered_until = max(covered_until, cycle.last_observed_at)
        for transition in cycle.transitions:
            left = max(start, transition.interval_started_at)
            right = min(end, transition.interval_ended_at)
            if left >= right:
                continue
            drop = transition.from_remaining_percent - transition.to_remaining_percent
            duration = (
                transition.interval_ended_at - transition.interval_started_at
            ).total_seconds()
            if drop <= 0 or duration <= 0:
                return None
            burned += drop * (right - left).total_seconds() / duration
    if covered_until < end - _MAX_SAMPLE_AGE:
        return None
    return burned
