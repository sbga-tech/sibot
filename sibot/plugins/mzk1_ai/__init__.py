"""NoneBot integration for Mzk1 AI."""

from nonebot import (
    get_bots,
    get_driver,
    get_plugin_config,
    logger,
    on_command,
    require,
)
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from .commands import AICommand, CommandUsageError, parse_ai_command
from .config import Config
from .forecast import load_pool_forecast
from .formatting import (
    format_help,
    format_quota,
    format_ranking,
    format_reset_credits,
)
from .monitor import QuotaMonitor
from .portal import PortalClient, PortalError
from .quota import extract_codex_weekly_accounts
from .reset import load_reset_credits
from .scope import is_target_group
from .storage import StateStore

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as localstore

__plugin_meta__ = PluginMetadata(
    name="Mzk1 AI",
    description="CPA Token 排名、Codex 周额度提醒、全池续航和重置机会查询。",
    usage="/ai, /ai rank [period], /ai quota, /ai reset",
    type="application",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

plugin_config = get_plugin_config(Config)
portal_client = PortalClient(
    str(plugin_config.mzk1_ai_portal_base_url),
    plugin_config.mzk1_ai_portal_admin_api_token,
)
state_store = StateStore(localstore.get_data_file("mzk1_ai", "state.json"))


class BotNotConnectedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("OneBot V11 is not connected")


async def _send_group_message(group_id: int, message: str) -> None:
    bots = [bot for bot in get_bots().values() if isinstance(bot, Bot)]
    if not bots:
        raise BotNotConnectedError
    await bots[0].send_group_msg(
        group_id=group_id,
        message=Message(MessageSegment.text(message)),
    )


quota_monitor = QuotaMonitor(
    portal=portal_client,
    store=state_store,
    config=plugin_config,
    send_notification=_send_group_message,
)


def _is_target_group(event: Event) -> bool:
    return is_target_group(event, plugin_config.mzk1_ai_group_id)


ai_command = on_command(
    "ai",
    rule=Rule(_is_target_group),
    priority=10,
    block=True,
)


@ai_command.handle()
async def handle_ai_command(
    matcher: Matcher,
    argument: Message = CommandArg(),
) -> None:
    try:
        command = parse_ai_command(argument.extract_plain_text())
    except CommandUsageError:
        message = format_help()
    else:
        message = await _execute_command(command)
    await matcher.finish(Message(MessageSegment.text(message)))


async def _execute_command(command: AICommand) -> str:
    try:
        return await _load_command_message(command)
    except PortalError as error:
        logger.error("Mzk1 AI command failed: {}", type(error).__name__)
    except Exception:  # noqa: BLE001
        logger.exception("Mzk1 AI command failed unexpectedly")
    return "查询失败，请稍后再试。"


async def _load_command_message(command: AICommand) -> str:
    if command.action == "rank" and command.period is not None:
        response = await portal_client.ranking(command.period)
        return format_ranking(response)
    if command.action == "quota":
        snapshot = await portal_client.quota()
        accounts = extract_codex_weekly_accounts(snapshot)
        activity = quota_monitor.weekly_window_activity()
        forecast = await load_pool_forecast(portal_client, accounts, activity)
        return format_quota(accounts, activity, forecast)
    if command.action == "reset":
        return format_reset_credits(await load_reset_credits(portal_client))
    return format_help()


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


@driver.on_bot_connect
async def wake_mzk1_ai(_bot: BaseBot) -> None:
    quota_monitor.wake()
