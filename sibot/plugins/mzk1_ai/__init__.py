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
from nonebot_plugin_alconna.uniseg import UniMessage

from .avatars import AvatarCache
from .chat import (
    alert_delivered,
    alert_target,
    chat_key,
    command_label,
    scope_of,
)
from .commands import AICommand, CommandUsageError, parse_ai_command
from .config import Config
from .forecast import load_forecasts
from .formatting import format_help, format_rank_no_data, format_rank_rejected
from .monitor import QuotaMonitor
from .portal import PortalBadRequestError, PortalClient, PortalError
from .quota import extract_accounts
from .render import render_quota, render_ranking, render_reset_credits
from .reset import load_reset_overview
from .storage import PendingNotification, StateStore

__plugin_meta__ = PluginMetadata(
    name="Mzk1 AI",
    description="CPA 用量排名、Codex/Claude 额度提醒、续航和重置机会查询。",
    usage="/ai rank [模型] [时间] [tokens|cost|req|cache], /ai quota, /ai reset",
    type="application",
    config=Config,
    supported_adapters={"~onebot.v11", "~discord", "~telegram"},
)

plugin_config = get_plugin_config(Config)
portal_client = PortalClient(
    str(plugin_config.mzk1_ai_portal_base_url),
    plugin_config.mzk1_ai_portal_admin_api_token,
)
state_store = StateStore(localstore.get_data_file("mzk1_ai", "state.json"))
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
        reply = await _execute_command(command, label)
    await reply.finish()


async def _execute_command(command: AICommand, label: str) -> UniMessage:
    try:
        return await _load_command_reply(command, label)
    except PortalError as error:
        logger.error("Mzk1 AI command failed: {}", type(error).__name__)
    except Exception:  # noqa: BLE001
        logger.exception("Mzk1 AI command failed unexpectedly")
    return UniMessage.text("查询失败，请稍后再试。")


async def _load_command_reply(command: AICommand, label: str) -> UniMessage:
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


driver = get_driver()


@driver.on_startup
async def start_mzk1_ai() -> None:
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
