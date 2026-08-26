"""Mzk1 AI configuration."""

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

_MIN_PERCENT = 0
_MAX_PERCENT = 100


class _MissingThresholdError(ValueError):
    def __init__(self) -> None:
        super().__init__("at least one Weekly alert threshold is required")


class _ThresholdRangeError(ValueError):
    def __init__(self) -> None:
        super().__init__("Weekly alert thresholds must be between 0 and 100")


class _ThresholdOrderError(ValueError):
    def __init__(self) -> None:
        super().__init__("Weekly alert thresholds must be unique and descending")


class Config(BaseModel):
    """NoneBot configuration consumed by the Mzk1 AI plugin."""

    model_config = ConfigDict(validate_default=True)

    mzk1_ai_group_id: int = Field(gt=0)
    mzk1_ai_portal_base_url: AnyHttpUrl = AnyHttpUrl(
        "http://cpa-portal:8080/api/admin/v1"
    )
    mzk1_ai_portal_admin_api_token: SecretStr
    mzk1_ai_codex_weekly_alert_thresholds: tuple[int, ...] = (50, 25, 10, 5, 0)

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
