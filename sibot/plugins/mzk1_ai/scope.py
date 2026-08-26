"""Event scope for the single allowed Mzk1 AI group."""

from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent


def is_target_group(event: Event, group_id: int) -> bool:
    """Return whether an event belongs to the configured QQ group."""
    return isinstance(event, GroupMessageEvent) and event.group_id == group_id
