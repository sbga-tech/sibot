"""CPA Portal response and internal quota models."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
)

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
    subscription_active_until: AwareDatetime | None = None


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


class QuotaCredentialsResponse(PortalModel):
    """Account index parsed independently of the Keeper quota cache."""

    credentials: list[QuotaCredential]


class QuotaSnapshot(QuotaCredentialsResponse):
    generated_at: datetime
    keeper: KeeperQuotaPayload


class ResetCredit(PortalModel):
    status: str
    expires_at: AwareDatetime | None = Field(default=None, alias="expiresAt")

    @field_validator("expires_at", mode="wrap")
    @classmethod
    def _parse_expiry(
        cls, value: object, handler: ValidatorFunctionWrapHandler
    ) -> datetime | None:
        # An unusable expiry must not hide the account's available count.
        try:
            return handler(value)
        except ValidationError:
            return None


class ResetCreditsResponse(PortalModel):
    auth_index: str = Field(alias="authIndex")
    available_count: int | None = Field(alias="availableCount", ge=0, strict=True)
    credits: list[ResetCredit]


@dataclass(frozen=True, slots=True)
class CodexAccountResetCredits:
    display_name: str
    response: ResetCreditsResponse | None


@dataclass(frozen=True, slots=True)
class CodexWeeklyQuota:
    remaining_percent: float
    exhausted: bool
    reset_at: datetime
    window_seconds: int
    window_role: Literal["primary", "secondary"]


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
    subscription_active_until: datetime | None = None


class QuotaRoutingCredential(PortalModel):
    credential_id: str
    provider: str
    disabled: bool
    status: str
    unavailable: bool
    next_retry_after: AwareDatetime | None = None
    weight: int | None = None


class QuotaRoutingSnapshot(PortalModel):
    generated_at: AwareDatetime
    credentials: list[QuotaRoutingCredential]


class QuotaHistoryWindow(PortalModel):
    window_role: Literal["primary", "secondary"]
    window_seconds: int


class QuotaHistoryTransition(PortalModel):
    from_remaining_percent: int = Field(ge=0, le=100)
    to_remaining_percent: int = Field(ge=0, le=100)
    interval_started_at: AwareDatetime
    interval_ended_at: AwareDatetime


class QuotaHistoryCycle(PortalModel):
    status: Literal["current", "completed"]
    window_seconds: int
    reset_at: AwareDatetime
    first_observed_at: AwareDatetime
    last_observed_at: AwareDatetime
    transitions: list[QuotaHistoryTransition]


class QuotaHistoryResponse(PortalModel):
    generated_at: AwareDatetime
    selected_window: QuotaHistoryWindow | None
    cycles: list[QuotaHistoryCycle]


@dataclass(frozen=True, slots=True)
class ForecastScenario:
    lookback_hours: int
    burn_percent_per_hour: float
    runway_hours: float | None
    reaches_reset: bool | None
    target_fraction: float | None


ForecastProblem: TypeAlias = Literal[
    "no_accounts",
    "missing_quota",
    "stale_quota",
    "unsupported_plans",
    "missing_routing",
    "unstarted_window",
    "missing_history",
]
ForecastWarning: TypeAlias = Literal[
    "routing_limited", "short_history", "subscription_expiring"
]


@dataclass(frozen=True, slots=True)
class PoolForecast:
    generated_at: datetime
    problem: ForecastProblem | None = None
    remaining_percent: float | None = None
    available_percent: float | None = None
    next_reset_at: datetime | None = None
    observed_at: datetime | None = None
    scenarios: tuple[ForecastScenario, ...] = ()
    warnings: tuple[ForecastWarning, ...] = ()
