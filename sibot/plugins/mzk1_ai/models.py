"""CPA Portal response and internal quota models."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

RankingPeriod: TypeAlias = Literal[
    "today",
    "yesterday",
    "current_month",
    "previous_month",
]


class PortalModel(BaseModel):
    """Base model tolerant of additive upstream fields."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class RankingUser(PortalModel):
    id: int
    github_id: int
    github_login: str
    github_name: str
    avatar_url: str


class RankingMetrics(PortalModel):
    total_tokens: int
    request_count: int
    cache_read_rate: int
    ttft_average: int
    latency_average: int
    peak_tpm: int
    peak_rpm: int


class RankingEntry(PortalModel):
    rank: int
    user: RankingUser
    value: int
    rate_numerator: int | None = None
    rate_denominator: int | None = None
    metrics: RankingMetrics | None = None


class RankingResponse(PortalModel):
    period: RankingPeriod
    period_key: str
    metric: str
    generated_at: datetime
    stale: bool
    entries: list[RankingEntry]


class QuotaCredential(PortalModel):
    credential_id: str
    display_name: str
    provider: str
    credential_type: str = Field(alias="type")
    disabled: bool


class KeeperVersion(PortalModel):
    version: str
    update_check_enabled: bool | None = Field(
        default=None,
        alias="updateCheckEnabled",
    )


class AutoRefreshSchedule(PortalModel):
    unit: str
    value: int


class AutoRefreshSettings(PortalModel):
    enabled: bool
    schedule: AutoRefreshSchedule | None


class QuotaWindow(PortalModel):
    duration: float | None = None
    unit: str | None = None
    seconds: int | None = None


class QuotaRow(PortalModel):
    key: str
    label: str | None = None
    scope: str | None = None
    metric: str | None = None
    group_key: str | None = Field(default=None, alias="groupKey")
    group_label: str | None = Field(default=None, alias="groupLabel")
    group_description: str | None = Field(default=None, alias="groupDescription")
    used: float | None = None
    limit: float | None = None
    remaining: float | None = None
    used_percent: float | None = Field(default=None, alias="usedPercent")
    remaining_fraction: float | None = Field(
        default=None,
        alias="remainingFraction",
    )
    allowed: bool | None = None
    limit_reached: bool | None = Field(default=None, alias="limitReached")
    window: QuotaWindow | None = None
    reset_at: datetime | None = Field(default=None, alias="resetAt")
    reset_after_seconds: int | None = Field(
        default=None,
        alias="resetAfterSeconds",
    )
    window_usage_tokens: int | None = None
    window_usage_cost: float | None = None


class SubscriptionInfo(PortalModel):
    provider: str
    plan: str
    tier_id: str | None = Field(default=None, alias="tierId")
    tier_name: str | None = Field(default=None, alias="tierName")


class QuotaCheckResponse(PortalModel):
    id: str
    quota: list[QuotaRow]
    subscription: SubscriptionInfo | None = None
    reset_credits_available: int | None = Field(
        default=None,
        alias="rateLimitResetCreditsAvailableCount",
    )


class CachedQuotaItem(PortalModel):
    auth_index: str
    file_name: str | None = None
    status: Literal["completed", "failed"]
    quota: QuotaCheckResponse | None = None
    error: str | None = None
    http_status_code: int | None = None
    expires_at: datetime | None = None
    refreshed_at: datetime | None = None


class QuotaCache(PortalModel):
    items: list[CachedQuotaItem]


class KeeperQuotaPayload(PortalModel):
    version: KeeperVersion
    auto_refresh: AutoRefreshSettings
    quota_cache: QuotaCache


class QuotaSnapshot(PortalModel):
    generated_at: datetime
    credentials: list[QuotaCredential]
    keeper: KeeperQuotaPayload


@dataclass(frozen=True, slots=True)
class CodexWeeklyQuota:
    remaining_percent: float
    exhausted: bool
    reset_at: datetime
    window_seconds: int


@dataclass(frozen=True, slots=True)
class CodexAccountQuota:
    credential_id: str
    display_name: str
    status: Literal["completed", "failed", "missing", "invalid_weekly"]
    refreshed_at: datetime | None
    http_status_code: int | None
    plan: str | None
    weekly: CodexWeeklyQuota | None
    reset_credits_available: int | None = None
