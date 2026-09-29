"""Subscription quota extraction for the deployed Keeper schema."""

import math
from collections.abc import Sequence
from typing import Literal

from .models import (
    PROVIDERS,
    AccountQuota,
    AccountStatus,
    CachedQuotaItem,
    ModelWindowQuota,
    Provider,
    QuotaCredential,
    QuotaRow,
    QuotaSnapshot,
    WindowQuota,
)

WEEKLY_WINDOW_SECONDS = 7 * 24 * 60 * 60
FIVE_HOUR_WINDOW_SECONDS = 5 * 60 * 60
_MAX_PERCENT = 100
_HTTP_UNAUTHORIZED = 401

# Keeper row keys for each provider's account-wide windows. Codex reports its
# windows by role, so the weekly one is identified by length.
_CODEX_MAIN_KEYS = {
    "rate_limit.primary_window": "primary",
    "rate_limit.secondary_window": "secondary",
}
_CLAUDE_FIVE_HOUR_KEY = "five_hour"
_CLAUDE_WEEKLY_KEY = "seven_day"
_CLAUDE_MODEL_KEYS = {"seven_day_opus": "Opus", "seven_day_sonnet": "Sonnet"}
_CLAUDE_EXTRA_USAGE_KEY = "extra_usage"


def extract_accounts(snapshot: QuotaSnapshot) -> list[AccountQuota]:
    """Extract enabled subscription accounts, isolating account-level errors."""

    cache_by_id = {item.auth_index: item for item in snapshot.keeper.quota_cache.items}
    return [
        _extract_account(
            provider, credential, cache_by_id.get(credential.credential_id)
        )
        for credential in snapshot.credentials
        if (provider := credential_provider(credential)) is not None
        and not credential.disabled
    ]


def credential_provider(credential: QuotaCredential) -> Provider | None:
    provider = credential.provider.lower()
    for known in PROVIDERS:
        if provider == known:
            return known
    return None


def is_login_invalid(account: AccountQuota) -> bool:
    """Keeper could not query the account because its login was rejected."""
    return account.status == "failed" and account.http_status_code == _HTTP_UNAUTHORIZED


def _extract_account(
    provider: Provider,
    credential: QuotaCredential,
    item: CachedQuotaItem | None,
) -> AccountQuota:
    if item is None:
        return _account(provider, credential, status="missing")

    plan = (
        item.quota.subscription.plan if item.quota and item.quota.subscription else None
    )
    if item.status == "failed":
        return _account(provider, credential, status="failed", item=item, plan=plan)
    if item.quota is None or item.refreshed_at is None:
        return _account(provider, credential, status="invalid", item=item, plan=plan)

    rows = item.quota.quota
    if provider == "codex":
        weekly_rows = [
            row
            for row in rows
            if row.key in _CODEX_MAIN_KEYS
            and row.scope == "window"
            and _window_seconds(row) == WEEKLY_WINDOW_SECONDS
        ]
        weekly = _window(weekly_rows[0]) if len(weekly_rows) == 1 else None
        five_hour = None
        models: tuple[ModelWindowQuota, ...] = ()
        extra_usage = None
    else:
        weekly = _single_window(rows, _CLAUDE_WEEKLY_KEY, WEEKLY_WINDOW_SECONDS)
        five_hour = _single_window(
            rows, _CLAUDE_FIVE_HOUR_KEY, FIVE_HOUR_WINDOW_SECONDS
        )
        models = tuple(
            ModelWindowQuota(label=label, remaining_percent=remaining)
            for row in rows
            if (label := _CLAUDE_MODEL_KEYS.get(row.key)) is not None
            and (remaining := _remaining(row)) is not None
        )
        extra_usage = next(
            (
                row.used_percent
                for row in rows
                if row.key == _CLAUDE_EXTRA_USAGE_KEY
                and row.allowed is True
                and row.used_percent is not None
                and math.isfinite(row.used_percent)
            ),
            None,
        )
    if weekly is None:
        # Anthropic omits resets_at until a window is first used; such an
        # account is idle, not broken. It still has no usable weekly window.
        status = "unstarted" if _is_unstarted(rows, provider) else "invalid"
        return _account(provider, credential, status=status, item=item, plan=plan)
    return _account(
        provider,
        credential,
        status="completed",
        item=item,
        plan=plan,
        weekly=weekly,
        five_hour=five_hour,
        models=models,
        extra_usage_percent=extra_usage,
    )


def _is_unstarted(rows: Sequence[QuotaRow], provider: Provider) -> bool:
    if provider != "claude":
        return False
    weekly = [
        row
        for row in rows
        if row.key == _CLAUDE_WEEKLY_KEY
        and row.scope == "window"
        and _window_seconds(row) == WEEKLY_WINDOW_SECONDS
    ]
    return (
        len(weekly) == 1 and weekly[0].reset_at is None and weekly[0].used_percent == 0
    )


def _single_window(
    rows: Sequence[QuotaRow], key: str, seconds: int
) -> WindowQuota | None:
    matches = [
        row
        for row in rows
        if row.key == key and row.scope == "window" and _window_seconds(row) == seconds
    ]
    return _window(matches[0]) if len(matches) == 1 else None


def _window(row: QuotaRow) -> WindowQuota | None:
    remaining = _remaining(row)
    seconds = _window_seconds(row)
    if remaining is None or row.reset_at is None or seconds is None:
        return None
    role: Literal["primary", "secondary"] = (
        "primary"
        if row.key in {"rate_limit.primary_window", _CLAUDE_FIVE_HOUR_KEY}
        else "secondary"
    )
    return WindowQuota(
        remaining_percent=remaining,
        # Keeper copies account-wide flags onto every row; a short-window
        # limit must not look like weekly exhaustion or recovery.
        exhausted=remaining <= 0,
        reset_at=row.reset_at,
        window_seconds=seconds,
        window_role=role,
    )


def _remaining(row: QuotaRow) -> float | None:
    used = row.used_percent
    if used is None or not math.isfinite(used) or not 0 <= used <= _MAX_PERCENT:
        return None
    return _MAX_PERCENT - used


def _window_seconds(row: QuotaRow) -> int | None:
    return row.window.seconds if row.window is not None else None


def _account(  # noqa: PLR0913
    provider: Provider,
    credential: QuotaCredential,
    *,
    status: AccountStatus,
    item: CachedQuotaItem | None = None,
    plan: str | None = None,
    weekly: WindowQuota | None = None,
    five_hour: WindowQuota | None = None,
    models: tuple[ModelWindowQuota, ...] = (),
    extra_usage_percent: float | None = None,
) -> AccountQuota:
    return AccountQuota(
        provider=provider,
        credential_id=credential.credential_id,
        display_name=credential.display_name,
        status=status,
        refreshed_at=item.refreshed_at if item else None,
        http_status_code=item.http_status_code if item else None,
        plan=plan,
        weekly=weekly,
        five_hour=five_hour,
        models=models,
        extra_usage_percent=extra_usage_percent,
        reset_credits_available=(
            item.quota.reset_credits_available if item and item.quota else None
        ),
        subscription_active_until=credential.subscription_active_until,
    )
