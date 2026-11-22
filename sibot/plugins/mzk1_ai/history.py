"""Weekly-quota history over the last few local days, from Keeper's quota history."""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from nonebot import logger

from .formatting import DISPLAY_TIMEZONE
from .models import (
    PROVIDERS,
    AccountHistory,
    AccountQuota,
    Provider,
    ProviderHistory,
    QuotaHistoryCycle,
    QuotaHistoryDays,
    QuotaHistoryOverview,
    QuotaHistoryResponse,
    RemainingSpan,
)
from .portal import PortalClient, PortalError
from .quota import PLAN_CAPACITIES, UNIT_PLANS, WEEKLY_WINDOW_SECONDS, extract_accounts

_HISTORY_QUERY_CONCURRENCY = 4
# Keeper writes a stable percentage at least every five minutes; a longer
# silence between cycles is drawn as missing data rather than bridged.
_MAX_BRIDGED_GAP = timedelta(minutes=30)


async def load_quota_history(
    portal: PortalClient, days: QuotaHistoryDays
) -> QuotaHistoryOverview:
    snapshot = await portal.quota()
    accounts = extract_accounts(snapshot)
    semaphore = asyncio.Semaphore(_HISTORY_QUERY_CONCURRENCY)
    histories = await asyncio.gather(
        *(_load_weekly_history(portal, account, semaphore) for account in accounts)
    )
    now = datetime.now(timezone.utc)
    today = now.astimezone(DISPLAY_TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = today - timedelta(days=days - 1)
    return QuotaHistoryOverview(
        days=days,
        start=start,
        generated_at=now,
        providers=tuple(
            _provider_history(
                provider,
                [
                    (account, history)
                    for account, history in zip(accounts, histories, strict=True)
                    if account.provider == provider
                ],
                start,
                days,
                now,
            )
            for provider in PROVIDERS
            if any(account.provider == provider for account in accounts)
        ),
    )


async def _load_weekly_history(
    portal: PortalClient, account: AccountQuota, semaphore: asyncio.Semaphore
) -> QuotaHistoryResponse | None:
    """The account's weekly-window history; None when the query failed."""
    async with semaphore:
        try:
            if account.weekly is not None:
                return await portal.quota_history(
                    account.credential_id, account.weekly.window_role
                )
            # Without a cached weekly window, find its role in the history.
            response = await portal.quota_history(account.credential_id)
            weekly = next(
                (
                    window
                    for window in response.windows
                    if window.window_seconds == WEEKLY_WINDOW_SECONDS
                ),
                None,
            )
            if weekly is None or response.selected_window == weekly:
                return response
            return await portal.quota_history(account.credential_id, weekly.window_role)
        except PortalError as error:
            logger.warning(
                "Mzk1 AI quota history query failed: {}", type(error).__name__
            )
            return None


def _provider_history(
    provider: Provider,
    accounts: Sequence[tuple[AccountQuota, QuotaHistoryResponse | None]],
    start: datetime,
    days: int,
    now: datetime,
) -> ProviderHistory:
    capacities = PLAN_CAPACITIES[provider]
    day_starts = [start + timedelta(days=index) for index in range(days)]
    daily = [0.0] * days
    results: list[AccountHistory] = []
    for account, history in accounts:
        if history is None:
            results.append(
                AccountHistory(
                    display_name=account.display_name,
                    plan=account.plan,
                    spans=None,
                    used_percent=0.0,
                    exhausted=False,
                )
            )
            continue
        spans, drops = _weekly_series(history.cycles, start, now)
        capacity = capacities.get(account.plan or "")
        if capacity is not None:
            for index, day in enumerate(day_starts):
                daily[index] += _used(drops, day, day + timedelta(days=1)) * capacity
        results.append(
            AccountHistory(
                display_name=account.display_name,
                plan=account.plan,
                spans=tuple(spans),
                used_percent=_used(drops, start, now),
                exhausted=any(span.remaining_percent == 0 for span in spans),
            )
        )
    return ProviderHistory(
        provider=provider,
        unit_plan=UNIT_PLANS[provider],
        daily_points=tuple(daily),
        accounts=tuple(results),
    )


_Drop = tuple[datetime, datetime, int]


def _weekly_series(
    cycles: Sequence[QuotaHistoryCycle], start: datetime, end: datetime
) -> tuple[list[RemainingSpan], list[_Drop]]:
    """Remaining-percent spans clipped to [start, end] and every observed drop.

    Spans cover only observed time; a reset shows as the next cycle starting
    full, never as negative usage.
    """
    spans: list[RemainingSpan] = []
    drops: list[_Drop] = []

    def add_span(span_start: datetime, span_end: datetime, remaining: int) -> None:
        left, right = max(span_start, start), min(span_end, end)
        if left < right:
            spans.append(RemainingSpan(left, right, remaining))

    previous: tuple[datetime, int] | None = None
    weekly = sorted(
        (cycle for cycle in cycles if cycle.window_seconds == WEEKLY_WINDOW_SECONDS),
        key=lambda cycle: cycle.first_observed_at,
    )
    for cycle in weekly:
        if cycle.last_observed_at < start:
            continue
        transitions = sorted(
            cycle.transitions, key=lambda transition: transition.interval_ended_at
        )
        value = (
            transitions[0].from_remaining_percent
            if transitions
            else cycle.first_remaining_percent
        )
        if value is None:
            continue
        if (
            previous is not None
            and cycle.first_observed_at - previous[0] <= _MAX_BRIDGED_GAP
        ):
            add_span(previous[0], cycle.first_observed_at, previous[1])
        cursor = cycle.first_observed_at
        for transition in transitions:
            at = transition.interval_ended_at
            add_span(cursor, at, value)
            drop = transition.from_remaining_percent - transition.to_remaining_percent
            if drop > 0:
                drops.append((transition.interval_started_at, at, drop))
            value = transition.to_remaining_percent
            cursor = at
        add_span(cursor, cycle.last_observed_at, value)
        previous = (cycle.last_observed_at, value)
    return spans, drops


def _used(drops: Sequence[_Drop], start: datetime, end: datetime) -> float:
    """Points dropped within [start, end], spreading each drop over its interval."""
    used = 0.0
    for interval_start, interval_end, drop in drops:
        duration = (interval_end - interval_start).total_seconds()
        if duration <= 0:
            used += drop if start <= interval_end < end else 0
            continue
        inside = (min(interval_end, end) - max(interval_start, start)).total_seconds()
        used += drop * max(0.0, inside) / duration
    return used
