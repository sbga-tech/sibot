"""On-demand forecasts from observed, reset-separated pool quota consumption."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from nonebot import logger

from .models import (
    PROVIDERS,
    AccountQuota,
    ForecastProblem,
    ForecastScenario,
    ForecastWarning,
    PoolForecast,
    Provider,
    QuotaHistoryCycle,
    QuotaHistoryResponse,
    QuotaRoutingCredential,
    QuotaRoutingSnapshot,
)
from .portal import PortalClient, PortalError
from .quota import WEEKLY_WINDOW_SECONDS

_HISTORY_QUERY_CONCURRENCY = 4
# Recent consumption predicts the next hours best; fall back to the shorter
# window when the longer one is not fully covered by history yet.
_LOOKBACK_HOURS = (12, 3)
_MAX_SAMPLE_AGE = timedelta(minutes=30)
_CYCLE_TIME_TOLERANCE = timedelta(minutes=2)
# CPA ends a quota cooldown slightly after the upstream reset (Claude adds up
# to 30 s of jitter); an account whose cooldown ends then still refills.
_ROUTING_RESET_TOLERANCE = timedelta(minutes=2)
_FULL_PERCENT = 100.0
# One account's full Plus weekly quota is 100 pool points.
_CODEX_UNIT_PLAN = "plus"
_CODEX_PLAN_CAPACITIES = {"plus": 1, "pro-5x": 5, "pro-20x": 20}


async def load_forecasts(
    portal: PortalClient,
    accounts: Sequence[AccountQuota],
    window_activity: Mapping[str, bool | None],
) -> dict[Provider, PoolForecast]:
    forecasts = await asyncio.gather(
        *(
            load_pool_forecast(
                portal,
                provider,
                [account for account in accounts if account.provider == provider],
                window_activity,
            )
            for provider in PROVIDERS
        )
    )
    return dict(zip(PROVIDERS, forecasts, strict=True))


async def load_pool_forecast(
    portal: PortalClient,
    provider: Provider,
    accounts: Sequence[AccountQuota],
    window_activity: Mapping[str, bool | None],
) -> PoolForecast:
    try:
        routing = await portal.quota_routing()
    except PortalError:
        return PoolForecast(
            provider=provider,
            generated_at=datetime.now(timezone.utc),
            problem="missing_routing",
        )
    semaphore = asyncio.Semaphore(_HISTORY_QUERY_CONCURRENCY)
    histories = await asyncio.gather(
        *(_load_history(portal, account, semaphore) for account in accounts)
    )
    return forecast_pool(
        provider,
        accounts,
        routing,
        dict(
            zip((account.credential_id for account in accounts), histories, strict=True)
        ),
        now=datetime.now(timezone.utc),
        window_activity=window_activity,
    )


async def _load_history(
    portal: PortalClient,
    account: AccountQuota,
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


def forecast_pool(  # noqa: PLR0913
    provider: Provider,
    accounts: Sequence[AccountQuota],
    routing: QuotaRoutingSnapshot,
    histories: Mapping[str, QuotaHistoryResponse | None],
    *,
    now: datetime,
    window_activity: Mapping[str, bool | None],
) -> PoolForecast:
    """Estimate the whole pool only up to its next known natural replenishment."""
    problem = _quota_problem(accounts, now)
    if problem is not None:
        return PoolForecast(provider=provider, generated_at=now, problem=problem)
    capacities = _capacities(provider, accounts)
    if capacities is None:
        return PoolForecast(
            provider=provider, generated_at=now, problem="unsupported_plans"
        )
    unit_plan, weights = capacities
    forecast = _pool_snapshot(
        provider,
        accounts,
        weights,
        unit_plan=unit_plan,
        routing=routing,
        now=now,
        window_activity=window_activity,
    )
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
    scenario = next(
        (
            scenario
            for hours in _LOOKBACK_HOURS
            if (
                scenario := _scenario(
                    forecast,
                    complete_histories,
                    accounts,
                    weights=weights,
                    routing=routing,
                    hours=hours,
                )
            )
            is not None
        ),
        None,
    )
    if scenario is None:
        return replace(forecast, problem="missing_history")
    observed_at = min(
        max(cycle.last_observed_at for cycle in history)
        for history in complete_histories
    )
    if forecast.observed_at is not None:
        observed_at = min(observed_at, forecast.observed_at)
    return replace(forecast, scenario=scenario, observed_at=observed_at)


def _quota_problem(
    accounts: Sequence[AccountQuota], now: datetime
) -> ForecastProblem | None:
    if not accounts:
        return "no_accounts"
    for account in accounts:
        weekly = account.weekly
        if account.status != "completed" or weekly is None:
            return "missing_quota"
        if (
            not _fresh(account.refreshed_at, now)
            or weekly.reset_at.utcoffset() is None
            or weekly.reset_at <= now
        ):
            return "stale_quota"
    return None


def _capacities(
    provider: Provider, accounts: Sequence[AccountQuota]
) -> tuple[str, dict[str, int]] | None:
    """Return the unit plan and each account's weekly capacity in unit plans."""
    if provider == "codex":
        if any(account.plan not in _CODEX_PLAN_CAPACITIES for account in accounts):
            return None
        return _CODEX_UNIT_PLAN, {
            account.credential_id: _CODEX_PLAN_CAPACITIES[account.plan or ""]
            for account in accounts
        }
    # Keeper reports Claude Max without its 5x/20x multiplier, so accounts can
    # only be pooled when they share one plan.
    plans = {account.plan for account in accounts}
    if len(plans) != 1 or None in plans:
        return None
    (plan,) = plans
    assert plan is not None
    return plan, {account.credential_id: 1 for account in accounts}


def _pool_snapshot(  # noqa: PLR0913
    provider: Provider,
    accounts: Sequence[AccountQuota],
    weights: Mapping[str, int],
    *,
    unit_plan: str,
    routing: QuotaRoutingSnapshot,
    now: datetime,
    window_activity: Mapping[str, bool | None],
) -> PoolForecast:
    statuses = {
        credential.credential_id: credential for credential in routing.credentials
    }
    if not _fresh(routing.generated_at, now) or any(
        account.credential_id not in statuses
        or statuses[account.credential_id].provider.lower() != provider
        for account in accounts
    ):
        return PoolForecast(
            provider=provider, generated_at=now, problem="missing_routing"
        )

    remaining = 0.0
    available = 0.0
    reset_times: list[datetime] = []
    for account in accounts:
        weekly = account.weekly
        assert weekly is not None  # Validated by _quota_problem.
        capacity = weights[account.credential_id]
        remaining += weekly.remaining_percent * capacity
        routable = _routable(statuses[account.credential_id], now)
        if routable:
            available += weekly.remaining_percent * capacity
        # Cooldowns can end at reset; disabled and zero-weight accounts cannot
        # contribute then.
        if _routable(
            statuses[account.credential_id], weekly.reset_at + _ROUTING_RESET_TOLERANCE
        ) and (
            weekly.remaining_percent < _FULL_PERCENT
            or window_activity.get(account.credential_id) is True
        ):
            reset_times.append(weekly.reset_at)
    next_reset = min(reset_times) if reset_times else None
    warnings: list[ForecastWarning] = []
    if available < remaining:
        warnings.append("routing_limited")
    return PoolForecast(
        provider=provider,
        generated_at=now,
        problem="unstarted_window" if next_reset is None else None,
        unit_plan=unit_plan,
        remaining_points=remaining,
        available_points=available,
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
    account: AccountQuota,
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


def _scenario(  # noqa: PLR0913
    forecast: PoolForecast,
    histories: Sequence[Sequence[QuotaHistoryCycle]],
    accounts: Sequence[AccountQuota],
    *,
    weights: Mapping[str, int],
    routing: QuotaRoutingSnapshot,
    hours: int,
) -> ForecastScenario | None:
    end = forecast.generated_at
    start = end - timedelta(hours=hours)
    burns = [_covered_burn(history, start, end) for history in histories]
    if any(burn is None for burn in burns):
        return None
    # Fixed unit-plan points: do not divide by today's pool size. An account
    # now in cooldown still contributes its observed consumption.
    rate = (
        sum(
            burn * weights[account.credential_id]
            for account, burn in zip(accounts, burns, strict=True)
            if burn is not None
        )
        / hours
    )
    if rate <= 0:
        return ForecastScenario(hours, rate, None, None, None)
    assert forecast.available_points is not None
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
        projected_balance += (
            max(0.0, account.weekly.remaining_percent - burn / hours * age_hours)
            * weights[account.credential_id]
        )
    runway_hours = projected_balance / rate
    return ForecastScenario(
        lookback_hours=hours,
        burn_points_per_hour=rate,
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
