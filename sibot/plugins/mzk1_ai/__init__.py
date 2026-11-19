"""NoneBot integration for Mzk1 AI."""

import asyncio
from datetime import datetime, timezone

from nonebot import get_driver, get_plugin_config, logger, on_command, require
from nonebot.adapters import Bot, Event, Message
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

require("nonebot_plugin_alconna")
require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as localstore
from nonebot_plugin_alconna.uniseg import At, UniMessage

from .avatars import AvatarCache
from .chat import (
    alert_delivered,
    alert_target,
    chat_key,
    command_label,
    require_scope,
    scope_of,
    telegram_username,
    user_key,
)
from .commands import AICommand, CommandUsageError, parse_ai_command
from .config import ChatScope, Config
from .forecast import load_forecasts
from .formatting import (
    MENTION_NOT_LINKED,
    NOT_LINKED,
    UNLINKED,
    USAGE_REJECTED,
    format_help,
    format_linked,
    format_rank_no_data,
    format_rank_rejected,
    format_self_not_linked,
    format_unknown_login,
    format_usage_empty,
)
from .monitor import QuotaMonitor
from .portal import PortalBadRequestError, PortalClient, PortalError
from .quota import extract_accounts
from .render import render_quota, render_ranking, render_reset_credits, render_usage
from .reset import load_reset_overview
from .storage import (
    ChatLink,
    LinkState,
    PendingNotification,
    PersistedState,
    StateStore,
)

__plugin_meta__ = PluginMetadata(
    name="Mzk1 AI",
    description="CPA 用量排名与个人用量、Codex/Claude 额度提醒、续航和重置机会查询。",
    usage=(
        "/ai rank [模型] [时间] [tokens|cost|req|cache], "
        "/ai usage [@成员|GitHub 用户名] [时间], /ai link <GitHub 用户名>, "
        "/ai unlink, /ai quota, /ai reset"
    ),
    type="application",
    config=Config,
    supported_adapters={"~onebot.v11", "~discord", "~telegram"},
)

plugin_config = get_plugin_config(Config)
portal_client = PortalClient(
    str(plugin_config.mzk1_ai_portal_base_url),
    plugin_config.mzk1_ai_portal_admin_api_token,
)
state_store = StateStore(
    localstore.get_data_file("mzk1_ai", "state.json"), PersistedState
)
link_store = StateStore(localstore.get_data_file("mzk1_ai", "links.json"), LinkState)
# Loaded on startup and changed only by command handlers.
link_state = LinkState()
avatar_cache = AvatarCache(localstore.get_cache_dir("mzk1_ai") / "avatars")
_targets = {target.key: target for target in plugin_config.mzk1_ai_targets}


async def _send_notification(notification: PendingNotification) -> None:
    target = alert_target(_targets[notification.target])
    await UniMessage.text(notification.message).send(target=target)


async def _notification_was_delivered(
    notification: PendingNotification, attempted_at: datetime
) -> bool:
    return await alert_delivered(
        _targets[notification.target], notification.message, attempted_at
    )


quota_monitor = QuotaMonitor(
    portal=portal_client,
    store=state_store,
    config=plugin_config,
    send_notification=_send_notification,
    check_notification=_notification_was_delivered,
)


def _is_target_chat(bot: Bot, event: Event) -> bool:
    if chat_key(bot, event) not in _targets:
        return False
    # Discord only shows the bot messages that mention it; the adapter strips
    # the mention and marks the event as addressed to the bot.
    return scope_of(bot) != "Discord" or event.is_tome()


ai_command = on_command("ai", rule=Rule(_is_target_chat), priority=10, block=True)


@ai_command.handle()
async def handle_ai_command(
    bot: Bot,
    event: Event,
    matcher: Matcher,
    argument: Message = CommandArg(),
) -> None:
    uni_argument = UniMessage.of(argument, bot=bot)
    text = uni_argument.extract_plain_text().strip()
    # Telegram groups write `/ai@bot_username`; the rest is the argument.
    if scope_of(bot) == "Telegram" and text.startswith("@"):
        addressee, _, text = text.partition(" ")
        if addressee[1:].lower() != str(getattr(bot, "username", "")).lower():
            await matcher.finish()
    label = command_label(bot)
    try:
        command = parse_ai_command(text)
    except CommandUsageError:
        reply = UniMessage.text(format_help(label))
    else:
        mentions = [segment.target for segment in uni_argument[At]]
        reply = await _execute_command(command, bot, event, label, mentions)
    await reply.finish()


async def _execute_command(
    command: AICommand, bot: Bot, event: Event, label: str, mentions: list[str]
) -> UniMessage:
    try:
        return await _load_command_reply(command, bot, event, label, mentions)
    except PortalError as error:
        logger.error("Mzk1 AI command failed: {}", type(error).__name__)
    except Exception:  # noqa: BLE001
        logger.exception("Mzk1 AI command failed unexpectedly")
    return UniMessage.text("查询失败，请稍后再试。")


async def _load_command_reply(
    command: AICommand, bot: Bot, event: Event, label: str, mentions: list[str]
) -> UniMessage:
    if command.action == "usage":
        return await _usage_reply(command, bot, event, label, mentions)
    if command.action in {"link", "unlink"}:
        return await _link_reply(command, bot, event)
    if command.action == "rank":
        return await _rank_reply(command, label)
    if command.action == "quota":
        return await _quota_reply()
    if command.action == "reset":
        overview = await load_reset_overview(portal_client)
        return UniMessage.image(
            raw=await asyncio.to_thread(
                render_reset_credits, overview, datetime.now(timezone.utc)
            )
        )
    return UniMessage.text(format_help(label))


async def _quota_reply() -> UniMessage:
    snapshot = await portal_client.quota()
    accounts = extract_accounts(snapshot)
    activity = quota_monitor.weekly_window_activity()
    forecasts = await load_forecasts(portal_client, accounts, activity)
    return UniMessage.image(
        raw=await asyncio.to_thread(
            render_quota, accounts, forecasts, activity, datetime.now(timezone.utc)
        )
    )


async def _rank_reply(command: AICommand, label: str) -> UniMessage:
    try:
        response = await portal_client.ranking(
            command.period, command.metric, command.matcher
        )
    except PortalBadRequestError:
        # Portal rejects periods past Keeper's lookback and long matchers.
        return UniMessage.text(
            format_rank_rejected(command.matcher, command.period, command.metric, label)
        )
    if not response.entries:
        return UniMessage.text(format_rank_no_data(response, label))
    avatars = await avatar_cache.load(
        [entry.user.avatar_url for entry in response.entries]
    )
    return UniMessage.image(
        raw=await asyncio.to_thread(render_ranking, response, avatars)
    )


async def _link_reply(command: AICommand, bot: Bot, event: Event) -> UniMessage:
    key = user_key(require_scope(bot), event.get_user_id())
    if command.action == "unlink":
        if link_state.links.pop(key, None) is None:
            return UniMessage.text(NOT_LINKED)
        await link_store.save(link_state)
        return UniMessage.text(UNLINKED)
    login = command.login or ""
    github_id = await _github_id(login)
    if github_id is None:
        return UniMessage.text(format_unknown_login(login))
    link_state.links[key] = ChatLink(
        github_id=github_id, username=telegram_username(event)
    )
    await link_store.save(link_state)
    return UniMessage.text(format_linked(login))


async def _usage_reply(
    command: AICommand,
    bot: Bot,
    event: Event,
    label: str,
    mentions: list[str],
) -> UniMessage:
    github_id = await _usage_subject(command, bot, event, label, mentions)
    if isinstance(github_id, UniMessage):
        return github_id
    try:
        profile = await portal_client.usage(github_id, command.period)
    except PortalBadRequestError:
        return UniMessage.text(USAGE_REJECTED)
    if profile.totals.requests == 0:
        return UniMessage.text(
            format_usage_empty(
                profile.user.github_login,
                profile.period,
                profile.start_date,
                profile.end_date,
            )
        )
    (avatar,) = await avatar_cache.load([profile.user.avatar_url])
    return UniMessage.image(raw=await asyncio.to_thread(render_usage, profile, avatar))


async def _usage_subject(
    command: AICommand,
    bot: Bot,
    event: Event,
    label: str,
    mentions: list[str],
) -> int | UniMessage:
    """GitHub id whose usage to show: the named login, the mentioned member, or
    the sender; otherwise the reply explaining why there is none."""
    scope = require_scope(bot)
    if mentions and command.login is not None:
        return UniMessage.text(format_help(label))
    if command.login is not None:
        github_id = await _github_id(command.login)
        reply = format_unknown_login(command.login)
    elif mentions:
        github_id = _linked_github_id(scope, mentions[0])
        reply = MENTION_NOT_LINKED
    else:
        github_id = _linked_github_id(scope, event.get_user_id())
        reply = format_self_not_linked(label)
    return UniMessage.text(reply) if github_id is None else github_id


async def _github_id(login: str) -> int | None:
    """The GitHub id of the Portal user with this login, ignoring case."""
    response = await portal_client.users()
    wanted = login.removeprefix("@").lower()
    return next(
        (
            user.github_id
            for user in response.users
            if user.github_login.lower() == wanted
        ),
        None,
    )


def _linked_github_id(scope: ChatScope, user: str) -> int | None:
    """The linked GitHub id of a chat user given by ID, or on Telegram by
    `@username`."""
    if scope == "Telegram" and user.startswith("@"):
        username = user[1:].lower()
        return next(
            (
                link.github_id
                for key, link in link_state.links.items()
                if key.startswith("Telegram:") and link.username == username
            ),
            None,
        )
    link = link_state.links.get(user_key(scope, user))
    return None if link is None else link.github_id


driver = get_driver()


@driver.on_startup
async def start_mzk1_ai() -> None:
    link_state.links.update((await link_store.load()).links)
    await quota_monitor.start()


@driver.on_shutdown
async def stop_mzk1_ai() -> None:
    try:
        await quota_monitor.stop()
    finally:
        await portal_client.close()
        await avatar_cache.close()


@driver.on_bot_connect
async def wake_mzk1_ai(_bot: Bot) -> None:
    quota_monitor.wake()
