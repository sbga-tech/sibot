"""Pure parser for the small `/ai` command surface."""

from dataclasses import dataclass
from typing import Literal

from .models import RankingPeriod

_PERIODS: dict[str, RankingPeriod] = {
    "today": "today",
    "yesterday": "yesterday",
    "month": "current_month",
    "last-month": "previous_month",
}
_RANK_MIN_ARGUMENTS = 2
_RANK_MAX_ARGUMENTS = 3


class CommandUsageError(ValueError):
    """The user supplied an unsupported Mzk1 AI command."""


@dataclass(frozen=True, slots=True)
class AICommand:
    action: Literal["help", "rank", "quota", "reset"]
    period: RankingPeriod | None = None
    matcher: str | None = None


def parse_ai_command(argument: str) -> AICommand:
    """Parse arguments following the `/ai` command."""
    tokens = argument.split()
    if not tokens:
        return AICommand(action="help")

    action = tokens[0].lower()
    if action == "quota" and len(tokens) == 1:
        return AICommand(action="quota")
    if action == "reset" and len(tokens) == 1:
        return AICommand(action="reset")
    if action == "rank":
        if len(tokens) == 1:
            return AICommand(action="rank", period="today")
        if not _RANK_MIN_ARGUMENTS <= len(tokens) <= _RANK_MAX_ARGUMENTS:
            raise CommandUsageError
        arguments = tokens[1:]
        period_tokens = [token for token in arguments if token.lower() in _PERIODS]
        if len(period_tokens) > 1:
            raise CommandUsageError
        period = _PERIODS[period_tokens[0].lower()] if period_tokens else "today"
        matchers = [token for token in arguments if token.lower() not in _PERIODS]
        if len(matchers) > 1:
            raise CommandUsageError
        return AICommand(
            action="rank", period=period, matcher=matchers[0] if matchers else None
        )
    raise CommandUsageError
