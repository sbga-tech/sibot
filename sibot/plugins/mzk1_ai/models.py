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
# `value` units: tokens for total_tokens, micro-USD for cost.
RankingMetric: TypeAlias = Literal["total_tokens", "cost"]
Provider: TypeAlias = Literal["codex", "claude"]
PROVIDERS: tuple[Provider, ...] = ("codex", "claude")
# "unstarted": the provider has not opened the weekly window yet (no reset time).
AccountStatus: TypeAlias = Literal[
    "completed", "failed", "missing", "invalid", "unstarted"
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


class RankingModel(PortalModel):
    model: str
    value: int


class RankingEntry(PortalModel):
    rank: int
    user: RankingUser
    value: int
    rate_numerator: int | None = None
    rate_denominator: int | None = None
    metrics: RankingMetrics | None = None
    models: list[RankingModel] = Field(default_factory=list)


class RankingResponse(PortalModel):
    period: RankingPeriod
    period_key: str
    metric: RankingMetric
    generated_at: datetime
    stale: bool
    matcher: str | None = None
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


class ClaudeResetGrant(PortalModel):
    """One Anthropic usage-limit reset benefit, as Keeper caches it."""

    model_config = ConfigDict(frozen=True)

    grant_id: str = Field(alias="id")
    label: str = ""
    resets_total: int = Field(alias="resetsTotal", ge=0, strict=True)
    resets_left: int = Field(alias="resetsLeft", ge=0, strict=True)
    starts_at: AwareDatetime | None = Field(default=None, alias="startsAt")
    ends_at: AwareDatetime | None = Field(default=None, alias="endsAt")
    clears: tuple[str, ...] = ()
    paused: bool = False
    use_requires_limit: bool = Field(default=False, alias="useRequiresLimit")


class ClaudeResetGrants(PortalModel):
    # Sum of resets left over every grant, usable now or not.
    available_count: int = Field(alias="availableCount", ge=0, strict=True)
    grants: tuple[ClaudeResetGrant, ...]


class QuotaCheckResponse(PortalModel):
    id: str
    quota: list[QuotaRow]
    subscription: SubscriptionInfo | None = None
    reset_credits_available: int | None = Field(
        default=None,
        alias="rateLimitResetCreditsAvailableCount",
    )
    claude_reset_grants: ClaudeResetGrants | None = Field(
        default=None,
        alias="claudeResetGrants",
    )

    @field_validator("claude_reset_grants", mode="wrap")
    @classmethod
    def _parse_claude_reset_grants(
        cls, value: object, handler: ValidatorFunctionWrapHandler
    ) -> ClaudeResetGrants | None:
        # Optional benefit data must not hide the account's quota windows.
        try:
            return handler(value)
        except ValidationError:
            return None


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
    token_estimates: dict[str, float] = Field(default_factory=dict)


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
class WindowQuota:
    remaining_percent: float
    exhausted: bool
    reset_at: datetime
    window_seconds: int
    window_role: Literal["primary", "secondary"]


@dataclass(frozen=True, slots=True)
class ModelWindowQuota:
    label: str
    remaining_percent: float


@dataclass(frozen=True, slots=True)
class AccountQuota:
    provider: Provider
    credential_id: str
    display_name: str
    status: AccountStatus
    refreshed_at: datetime | None
    http_status_code: int | None
    plan: str | None
    weekly: WindowQuota | None
    tokens_per_percent: float | None = None
    five_hour: WindowQuota | None = None
    models: tuple[ModelWindowQuota, ...] = ()
    extra_usage_percent: float | None = None
    reset_credits_available: int | None = None
    # None when Keeper has no grant data for the account (always for Codex).
    claude_reset_grants: tuple[ClaudeResetGrant, ...] | None = None
    subscription_active_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResetOverview:
    # None when that provider's part of the query failed.
    codex: tuple[CodexAccountResetCredits, ...] | None
    claude: tuple[AccountQuota, ...] | None


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
    rate_windows: tuple[int, ...]
    burn_points_per_hour: float
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
ForecastWarning: TypeAlias = Literal["routing_limited"]


@dataclass(frozen=True, slots=True)
class PoolForecast:
    provider: Provider
    generated_at: datetime
    problem: ForecastProblem | None = None
    # Plan whose full weekly quota counts as 100 pool points.
    unit_plan: str | None = None
    remaining_points: float | None = None
    available_points: float | None = None
    next_reset_at: datetime | None = None
    observed_at: datetime | None = None
    estimated_tokens: float | None = None
    scenario: ForecastScenario | None = None
    warnings: tuple[ForecastWarning, ...] = ()
