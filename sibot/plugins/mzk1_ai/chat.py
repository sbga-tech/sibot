"""What differs between chat platforms: how `/ai` is written, who sent it, and
whether a sent alert can be found again. Messages themselves go through
uniseg, which converts them for each adapter."""

from datetime import datetime

from nonebot.adapters import Bot, Event
from nonebot.adapters.discord import Bot as DiscordBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotSegment
from nonebot.adapters.telegram import Bot as TelegramBot
from nonebot.adapters.telegram.event import GroupMessageEvent as TelegramGroupEvent
from nonebot.compat import type_validate_python
from nonebot_plugin_alconna.uniseg import Target, get_target

from .config import ChatScope, ChatTarget

# Adapter name -> chat platform, as nonebot-plugin-alconna names it.
_SCOPES: dict[str, ChatScope] = {
    "OneBot V11": "QQClient",
    "Discord": "Discord",
    "Telegram": "Telegram",
}
_ADAPTERS: dict[ChatScope, str] = {scope: name for name, scope in _SCOPES.items()}
_HISTORY_LIMIT = 100
_DISCORD_EPOCH_MS = 1_420_070_400_000
_SNOWFLAKE_TIMESTAMP_SHIFT = 22


class NotificationHistoryIncompleteError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Chat history does not cover the notification attempt")


def scope_of(bot: Bot) -> ChatScope | None:
    return _SCOPES.get(bot.adapter.get_name())


def require_scope(bot: Bot) -> ChatScope:
    """The platform of a bot that passed the `/ai` rule."""
    scope = scope_of(bot)
    if scope is None:
        raise TypeError(bot.adapter.get_name())
    return scope


def chat_key(bot: Bot, event: Event) -> str | None:
    """`scope:chat_id` of a group chat or channel message, else None."""
    scope = scope_of(bot)
    if scope is None:
        return None
    try:
        target = get_target(event, bot)
    except (NotImplementedError, AssertionError):
        return None
    if target.private:
        return None
    return f"{scope}:{target.id}"


def user_key(scope: ChatScope, user_id: str) -> str:
    return f"{scope}:{user_id}"


def command_label(bot: Bot) -> str:
    """How users write `/ai` on this platform, for help text."""
    if isinstance(bot, DiscordBot):
        return f"@{bot.self_info.username} /ai"
    if isinstance(bot, TelegramBot):
        return f"/ai@{bot.username}"
    return "/ai"


def telegram_username(event: Event) -> str | None:
    """The sender's Telegram username, lowercase, if any."""
    if isinstance(event, TelegramGroupEvent) and event.from_.username:
        return event.from_.username.lower()
    return None


def alert_target(target: ChatTarget) -> Target:
    return Target(
        target.id,
        channel=target.scope == "Discord",
        adapter=_ADAPTERS[target.scope],
    )


async def alert_delivered(target: ChatTarget, text: str, since: datetime) -> bool:
    """Whether the bot has posted `text` to the chat since `since`.

    Telegram bots cannot read chat history, so a Telegram alert whose
    acknowledgement was lost is sent again: a rare duplicate is preferred over
    a missed alert.
    """
    if target.scope == "Telegram":
        return False
    bot = await alert_target(target).select()
    if target.scope == "Discord":
        return await _discord_delivered(bot, int(target.id), text, since)
    return await _onebot_delivered(bot, int(target.id), text, since)


async def _onebot_delivered(
    bot: Bot, group_id: int, text: str, since: datetime
) -> bool:
    since_ts = int(since.timestamp())
    expected = OneBotMessage(OneBotSegment.text(text))
    cursor = 0
    while True:
        result = await bot.call_api(
            "get_group_msg_history",
            group_id=group_id,
            count=_HISTORY_LIMIT,
            message_seq=cursor,
        )
        messages = result["messages"]
        if not messages:
            return False
        for item in messages:
            if item["time"] < since_ts or str(item["sender"]["user_id"]) != bot.self_id:
                continue
            # History holds segment dicts or CQ strings; Message() rejects
            # dict segments, the pydantic validator accepts both.
            if type_validate_python(OneBotMessage, item["message"]) == expected:
                return True
        if (
            len(messages) < _HISTORY_LIMIT
            or min(item["time"] for item in messages) < since_ts
        ):
            return False
        oldest = min(item["message_seq"] for item in messages)
        if cursor and oldest >= cursor:
            raise NotificationHistoryIncompleteError
        cursor = oldest


async def _discord_delivered(
    bot: Bot, channel_id: int, text: str, since: datetime
) -> bool:
    if not isinstance(bot, DiscordBot):
        raise TypeError(type(bot).__name__)
    self_id = bot.self_info.id
    after = (
        int(since.timestamp() * 1000) - _DISCORD_EPOCH_MS
    ) << _SNOWFLAKE_TIMESTAMP_SHIFT
    while True:
        messages = await bot.get_channel_messages(
            channel_id=channel_id, after=after, limit=_HISTORY_LIMIT
        )
        if any(
            message.author.id == self_id and message.content == text
            for message in messages
        ):
            return True
        if len(messages) < _HISTORY_LIMIT:
            return False
        after = max(message.id for message in messages)
