"""Compact Chinese messages for narrow QQ mobile layouts."""

from datetime import datetime
from zoneinfo import ZoneInfo

from .alerts import AlertEvent
from .models import CodexAccountQuota, RankingResponse

_DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
_HTTP_UNAUTHORIZED = 401
_PERIOD_LABELS = {
    "today": "今日",
    "yesterday": "昨日",
    "current_month": "本月",
    "previous_month": "上月",
}


def format_help() -> str:
    return "\n".join(
        (
            "可用指令：",
            "/ai rank [today|yesterday|month|last-month]",
            "/ai quota",
        )
    )


def format_ranking(ranking: RankingResponse) -> str:
    period = _PERIOD_LABELS[ranking.period]
    lines = [f"Token 用量排名（{period}）"]
    if not ranking.entries:
        lines.append("暂无数据")
    else:
        lines.extend(
            f"{entry.rank}. {entry.user.github_login}：{entry.value:,}"
            for entry in ranking.entries
        )
    if ranking.stale:
        lines.append("数据可能已过期")
    return "\n".join(lines)


def format_quota(accounts: list[CodexAccountQuota]) -> str:
    if not accounts:
        return "Codex 周额度\n暂无可用账号"

    lines = ["Codex 周额度"]
    for account in accounts:
        lines.extend(_format_account_quota(account))
    return "\n".join(lines)


def _format_account_quota(account: CodexAccountQuota) -> tuple[str, ...]:
    plan = f" [{account.plan}]" if account.plan else ""
    name = f"{account.display_name}{plan}"
    if account.status == "failed":
        if account.http_status_code == _HTTP_UNAUTHORIZED:
            return (f"{name}：登录无效或已过期",)
        return (f"{name}：额度暂时不可用",)
    if account.status in {"missing", "invalid_weekly"} or account.weekly is None:
        return (f"{name}：额度暂时不可用",)

    reset_at = _format_time(account.weekly.reset_at)
    if account.weekly.exhausted:
        status = "已用完"
    else:
        status = f"剩余{_format_percent(account.weekly.remaining_percent)}"
    return (name, f"{status}，{reset_at}重置")


def format_alert_batch(events: list[AlertEvent]) -> str:
    return "\n".join(("Codex 周额度提醒", *map(_format_alert, events)))


def _format_alert(event: AlertEvent) -> str:
    if event.kind == "weekly_low":
        return (
            f"{event.display_name}：周额度只剩"
            f"{_format_percent(event.remaining_percent)}，"
            f"{_format_time(event.reset_at)}重置"
        )
    if event.kind == "weekly_exhausted":
        return f"{event.display_name}：周额度已用完，{_format_time(event.reset_at)}重置"
    if event.kind == "weekly_reset":
        return (
            f"{event.display_name}：周额度已重置，"
            f"当前剩余{_format_percent(event.remaining_percent)}，"
            f"下次{_format_time(event.reset_at)}重置"
        )
    return f"{event.display_name}：登录已失效"


def _format_percent(value: float) -> str:
    return f"{value:g}%"


def _format_time(value: datetime) -> str:
    local = value.astimezone(_DISPLAY_TIMEZONE)
    return f"{local.month}月{local.day}日{local:%H:%M}"
