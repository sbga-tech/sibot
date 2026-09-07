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
_RANK_WITH_PERIOD_ARGUMENTS = 2


class CommandUsageError(ValueError):
    """The user supplied an unsupported Mzk1 AI command."""


@dataclass(frozen=True, slots=True)
class AICommand:
    action: Literal["help", "rank", "quota", "reset"]
    period: RankingPeriod | None = None


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
        if len(tokens) == _RANK_WITH_PERIOD_ARGUMENTS and tokens[1].lower() in _PERIODS:
            return AICommand(action="rank", period=_PERIODS[tokens[1].lower()])
    raise CommandUsageError
