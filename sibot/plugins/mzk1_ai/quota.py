"""Codex Weekly quota extraction for the deployed Keeper schema."""

import math
from typing import Literal

from .models import (
    CachedQuotaItem,
    CodexAccountQuota,
    CodexWeeklyQuota,
    QuotaCredential,
    QuotaSnapshot,
)

EXPECTED_KEEPER_VERSION = "v1.14.8"
WEEKLY_WINDOW_SECONDS = 7 * 24 * 60 * 60
_MAX_PERCENT = 100
_MAIN_RATE_LIMIT_KEYS = {
    "rate_limit.primary_window",
    "rate_limit.secondary_window",
}


class UnsupportedKeeperVersionError(RuntimeError):
    """Keeper is not the exact version this internal parser targets."""

    def __init__(self, actual_version: str) -> None:
        super().__init__(
            f"expected Keeper {EXPECTED_KEEPER_VERSION}, got {actual_version}"
        )


def extract_codex_weekly_accounts(
    snapshot: QuotaSnapshot,
) -> list[CodexAccountQuota]:
    """Extract active Codex accounts and isolate account-level schema errors."""
    actual_version = snapshot.keeper.version.version
    if actual_version != EXPECTED_KEEPER_VERSION:
        raise UnsupportedKeeperVersionError(actual_version)

    cache_by_id = {item.auth_index: item for item in snapshot.keeper.quota_cache.items}
    return [
        _extract_account(credential, cache_by_id.get(credential.credential_id))
        for credential in snapshot.credentials
        if credential.provider.lower() == "codex" and not credential.disabled
    ]


def _extract_account(
    credential: QuotaCredential,
    item: CachedQuotaItem | None,
) -> CodexAccountQuota:
    if item is None:
        return _account(credential, status="missing")

    plan = (
        item.quota.subscription.plan if item.quota and item.quota.subscription else None
    )
    if item.status == "failed":
        return _account(
            credential,
            status="failed",
            item=item,
            plan=plan,
        )
    if item.quota is None or item.refreshed_at is None:
        return _invalid_weekly_account(credential, item, plan)

    candidates = [
        row
        for row in item.quota.quota
        if row.key in _MAIN_RATE_LIMIT_KEYS
        and row.scope == "window"
        and row.window is not None
        and row.window.seconds == WEEKLY_WINDOW_SECONDS
    ]
    if len(candidates) != 1:
        return _invalid_weekly_account(credential, item, plan)

    row = candidates[0]
    if (
        row.used_percent is None
        or not math.isfinite(row.used_percent)
        or not 0 <= row.used_percent <= _MAX_PERCENT
        or row.reset_at is None
    ):
        return _invalid_weekly_account(credential, item, plan)

    remaining_percent = _MAX_PERCENT - row.used_percent
    exhausted = (
        row.limit_reached is True or row.allowed is False or remaining_percent <= 0
    )
    return _account(
        credential,
        status="completed",
        item=item,
        plan=plan,
        weekly=CodexWeeklyQuota(
            remaining_percent=remaining_percent,
            exhausted=exhausted,
            reset_at=row.reset_at,
            window_seconds=WEEKLY_WINDOW_SECONDS,
        ),
    )


def _invalid_weekly_account(
    credential: QuotaCredential,
    item: CachedQuotaItem,
    plan: str | None,
) -> CodexAccountQuota:
    return _account(
        credential,
        status="invalid_weekly",
        item=item,
        plan=plan,
    )


def _account(
    credential: QuotaCredential,
    *,
    status: Literal["completed", "failed", "missing", "invalid_weekly"],
    item: CachedQuotaItem | None = None,
    plan: str | None = None,
    weekly: CodexWeeklyQuota | None = None,
) -> CodexAccountQuota:
    return CodexAccountQuota(
        credential_id=credential.credential_id,
        display_name=credential.display_name,
        status=status,
        refreshed_at=item.refreshed_at if item else None,
        http_status_code=item.http_status_code if item else None,
        plan=plan,
        weekly=weekly,
        reset_credits_available=(
            item.quota.reset_credits_available if item and item.quota else None
        ),
    )
