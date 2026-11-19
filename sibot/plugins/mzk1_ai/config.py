"""Mzk1 AI configuration."""

from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

# Chat platforms as nonebot-plugin-alconna names them (`SupportScope`): any
# unofficial QQ client (OneBot V11 here), Discord, and Telegram.
ChatScope = Literal["QQClient", "Discord", "Telegram"]

_MIN_PERCENT = 0
_MAX_PERCENT = 100


class _MissingTargetError(ValueError):
    def __init__(self) -> None:
        super().__init__("at least one chat target is required")


class _MissingThresholdError(ValueError):
    def __init__(self) -> None:
        super().__init__("at least one Weekly alert threshold is required")


class _ThresholdRangeError(ValueError):
    def __init__(self) -> None:
        super().__init__("Weekly alert thresholds must be between 0 and 100")


class _ThresholdOrderError(ValueError):
    def __init__(self) -> None:
        super().__init__("Weekly alert thresholds must be unique and descending")


class ChatTarget(BaseModel):
    """A chat that answers `/ai` and, unless disabled, receives alerts."""

    model_config = ConfigDict(frozen=True, extra="forbid", coerce_numbers_to_str=True)

    scope: ChatScope
    # QQ group number, Discord channel ID, or Telegram chat ID.
    id: str = Field(min_length=1)
    alerts: bool = True

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.id}"


class Config(BaseModel):
    """NoneBot configuration consumed by the Mzk1 AI plugin."""

    model_config = ConfigDict(validate_default=True)

    mzk1_ai_targets: tuple[ChatTarget, ...]
    mzk1_ai_portal_base_url: AnyHttpUrl = AnyHttpUrl(
        "http://cpa-portal:8080/api/admin/v1"
    )
    mzk1_ai_portal_admin_api_token: SecretStr
    # Historical name; the thresholds apply to every provider's weekly quota.
    mzk1_ai_codex_weekly_alert_thresholds: tuple[int, ...] = (50, 25, 10, 5, 0)
    mzk1_ai_subscription_alert_hours: tuple[Annotated[int, Field(gt=0)], ...] = Field(
        default=(72, 24), min_length=1
    )

    @field_validator("mzk1_ai_targets")
    @classmethod
    def validate_targets(cls, value: tuple[ChatTarget, ...]) -> tuple[ChatTarget, ...]:
        # A Field(min_length=...) would hide the tuple type from NoneBot, which
        # then skips JSON-decoding the environment variable.
        if not value:
            raise _MissingTargetError
        return value

    @field_validator("mzk1_ai_codex_weekly_alert_thresholds")
    @classmethod
    def validate_thresholds(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise _MissingThresholdError
        if any(
            threshold < _MIN_PERCENT or threshold > _MAX_PERCENT for threshold in value
        ):
            raise _ThresholdRangeError
        if tuple(sorted(set(value), reverse=True)) != value:
            raise _ThresholdOrderError
        return value
