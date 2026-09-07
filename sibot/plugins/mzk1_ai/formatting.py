"""Compact Chinese messages for narrow QQ mobile layouts."""

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from .alerts import AlertEvent
from .models import CodexAccountQuota, CodexAccountResetCredits, RankingResponse

_DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
_HTTP_UNAUTHORIZED = 401
_FULL_PERCENT = 100.0
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
            "/ai reset",
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


def format_quota(
    accounts: list[CodexAccountQuota],
    window_activity: Mapping[str, bool | None] | None = None,
) -> str:
    if not accounts:
        return "Codex 周额度\n暂无可用账号"

    activity = window_activity or {}
    lines = ["Codex 周额度"]
    for account in accounts:
        lines.extend(
            _format_account_quota(
                account,
                window_active=activity.get(account.credential_id),
            )
        )
    return "\n".join(lines)


def _format_account_quota(
    account: CodexAccountQuota,
    *,
    window_active: bool | None,
) -> tuple[str, ...]:
    plan = f" [{account.plan}]" if account.plan else ""
    name = f"{account.display_name}{plan}"
    if account.status == "failed":
        if account.http_status_code == _HTTP_UNAUTHORIZED:
            return (f"{name}：登录无效或已过期",)
        return (f"{name}：额度暂时不可用",)
    if account.status in {"missing", "invalid_weekly"} or account.weekly is None:
        return (f"{name}：额度暂时不可用",)

    if account.weekly.exhausted:
        status = "已用完"
    else:
        status = f"剩余{_format_percent(account.weekly.remaining_percent)}"
    if (
        account.weekly.exhausted
        or account.weekly.remaining_percent < _FULL_PERCENT
        or window_active is True
    ):
        reset_at = _format_time(account.weekly.reset_at)
        return (name, f"{status}，{reset_at}重置")
    return (name, status)


def format_reset_credits(accounts: list[CodexAccountResetCredits]) -> str:
    if not accounts:
        return "Codex 重置机会\n暂无可用账号"
    return "Codex 重置机会\n" + "\n\n".join(
        _format_account_reset_credits(account) for account in accounts
    )


def _format_account_reset_credits(account: CodexAccountResetCredits) -> str:
    response = account.response
    if response is None:
        return f"{account.display_name}：查询失败"

    count = response.available_count
    status = "可用次数未知" if count is None else f"可用 {count} 次"
    lines = [f"{account.display_name}：{status}"]
    if count == 0:
        return lines[0]

    available = [credit for credit in response.credits if credit.status == "available"]
    expiries = Counter(
        credit.expires_at.astimezone(_DISPLAY_TIMEZONE)
        for credit in available
        if credit.expires_at is not None
    )
    lines.extend(
        f"{amount} 次于 {_format_time(expiry)} 过期"
        for expiry, amount in sorted(expiries.items())
    )
    known_count = sum(expiries.values())
    if not expiries:
        lines.append("过期时间未知")
    elif known_count < len(available) or (count is not None and known_count < count):
        lines.append("部分过期时间未知")
    return "\n".join(lines)


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
        if event.source == "scheduled":
            action = "周额度已正常重置"
        elif event.source == "reset_credit":
            action = "已使用重置机会"
        else:
            action = "OpenAI已重置周额度"
        return (
            f"{event.display_name}：{action}，"
            f"当前剩余{_format_percent(event.remaining_percent)}，"
            f"下次{_format_time(event.reset_at)}重置"
        )
    if event.kind == "reset_credit_increased":
        return (
            f"{event.display_name}：新增{event.added_count}次重置机会，"
            f"当前可用{event.available_count}次"
        )
    return f"{event.display_name}：登录已失效"


def _format_percent(value: float) -> str:
    return f"{value:g}%"


def _format_time(value: datetime) -> str:
    local = value.astimezone(_DISPLAY_TIMEZONE)
    return f"{local.month}月{local.day}日{local:%H:%M}"
