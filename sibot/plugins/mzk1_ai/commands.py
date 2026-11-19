"""Pure parser for the small `/ai` command surface."""

import re
from dataclasses import dataclass
from typing import Literal

from .models import RankingMetric, RankingPeriod

# Shape of Portal's period syntax; Portal checks the range itself.
_PERIOD = re.compile(r"[dwm](?:-\d{1,4})?|\d{1,4}[dwm]", re.IGNORECASE)
_METRICS: dict[str, RankingMetric] = {
    "tokens": "tokens",
    "cost": "cost",
    "req": "req",
    "cache": "cache",
}
_RANK_MAX_ARGUMENTS = 3
_USAGE_MAX_ARGUMENTS = 2
_USAGE_DEFAULT_PERIOD = "7d"
# Actions that take no arguments.
_BARE_ACTIONS: dict[str, Literal["quota", "reset", "unlink"]] = {
    "quota": "quota",
    "reset": "reset",
    "unlink": "unlink",
}


class CommandUsageError(ValueError):
    """The user supplied an unsupported Mzk1 AI command."""


@dataclass(frozen=True, slots=True)
class AICommand:
    action: Literal["help", "rank", "usage", "link", "unlink", "quota", "reset"]
    period: RankingPeriod = "d"
    metric: RankingMetric = "tokens"
    matcher: str | None = None
    # GitHub login: whose usage to show, or whom to link to.
    login: str | None = None


def parse_ai_command(argument: str) -> AICommand:
    """Parse arguments following the `/ai` command."""
    tokens = argument.split()
    if not tokens:
        return AICommand(action="help")

    action = tokens[0].lower()
    bare = _BARE_ACTIONS.get(action)
    if bare is not None and len(tokens) == 1:
        return AICommand(action=bare)
    if action == "link" and len(tokens[1:]) == 1:
        return AICommand(action="link", login=tokens[1])
    if action == "usage":
        return _parse_usage(tokens[1:])
    if action == "rank":
        return _parse_rank(tokens[1:])
    raise CommandUsageError


def _parse_usage(arguments: list[str]) -> AICommand:
    """`/ai usage [login] [period]` in any order; a mention replaces the login."""
    if len(arguments) > _USAGE_MAX_ARGUMENTS:
        raise CommandUsageError
    periods = [
        argument.lower() for argument in arguments if _PERIOD.fullmatch(argument)
    ]
    logins = [argument for argument in arguments if not _PERIOD.fullmatch(argument)]
    if len(periods) > 1 or len(logins) > 1:
        raise CommandUsageError
    return AICommand(
        action="usage",
        period=periods[0] if periods else _USAGE_DEFAULT_PERIOD,
        login=logins[0] if logins else None,
    )


def _parse_rank(arguments: list[str]) -> AICommand:
    """`/ai rank [model] [period] [metric]` in any order."""
    if len(arguments) > _RANK_MAX_ARGUMENTS:
        raise CommandUsageError
    periods: list[str] = []
    metrics: list[RankingMetric] = []
    matchers: list[str] = []
    for argument in arguments:
        lowered = argument.lower()
        metric = _METRICS.get(lowered)
        if metric is not None:
            metrics.append(metric)
        elif _PERIOD.fullmatch(argument):
            periods.append(lowered)
        else:
            matchers.append(argument)
    if len(periods) > 1 or len(metrics) > 1 or len(matchers) > 1:
        raise CommandUsageError
    return AICommand(
        action="rank",
        period=periods[0] if periods else "d",
        metric=metrics[0] if metrics else "tokens",
        matcher=matchers[0] if matchers else None,
    )
