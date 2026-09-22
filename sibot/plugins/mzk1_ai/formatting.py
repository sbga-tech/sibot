"""Compact Chinese messages for narrow QQ mobile layouts."""

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .alerts import AlertEvent
from .models import (
    CodexAccountQuota,
    CodexAccountResetCredits,
    ForecastScenario,
    PoolForecast,
    RankingResponse,
)

_DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
_HTTP_UNAUTHORIZED = 401
_FULL_PERCENT = 100.0
_HOURS_PER_DAY = 24
_SHORT_RUNWAY_HOURS = 48
_PERIOD_LABELS = {
    "today": "今日",
    "yesterday": "昨日",
    "current_month": "本月",
    "previous_month": "上月",
}
_FORECAST_PROBLEMS = {
    "no_accounts": "暂无 Codex 账号",
    "missing_quota": "额度数据不完整",
    "stale_quota": "额度数据已过期",
    "unsupported_plans": "订阅额度无法换算",
    "missing_routing": "调度状态不完整",
    "unstarted_window": "暂无重置时间",
    "missing_history": "历史数据不足",
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
    forecast: PoolForecast | None = None,
) -> str:
    if not accounts:
        return "Codex 额度\n暂无可用账号"

    activity = window_activity or {}
    lines = ["Codex 额度"]
    if forecast is not None:
        lines.extend(_format_forecast_summary(forecast))
    lines.append("")
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
        message = (
            "登录失效"
            if account.http_status_code == _HTTP_UNAUTHORIZED
            else "额度暂不可用"
        )
        return (name, message)
    if account.status in {"missing", "invalid_weekly"} or account.weekly is None:
        return (name, "额度暂不可用")

    weekly = account.weekly
    if weekly.exhausted:
        status = "已用完"
    else:
        status = f"剩余{_format_percent(weekly.remaining_percent)}"
    if (
        weekly.exhausted
        or weekly.remaining_percent < _FULL_PERCENT
        or window_active is True
    ):
        status += f"，{_format_time(weekly.reset_at)}重置"
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
    return "\n".join(("Codex 提醒", *map(_format_alert, events)))


def _format_alert(event: AlertEvent) -> str:
    if event.kind == "subscription_expiring":
        return (
            f"{event.display_name}：订阅将在{_format_time(event.active_until)}到期，"
            f"剩余不超过{event.threshold_hours}小时，记得续费。"
        )
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


def _format_forecast_summary(forecast: PoolForecast) -> list[str]:
    lines = _format_forecast_balance(forecast)
    if forecast.problem is not None:
        lines.append(_FORECAST_PROBLEMS[forecast.problem])
    else:
        lines.extend(_format_forecast_scenarios(forecast))
    if "routing_limited" in forecast.warnings:
        lines.append("部分账号暂不可用")
    if "subscription_expiring" in forecast.warnings:
        lines.append("有订阅将在重置前到期")
    return lines


def _format_forecast_balance(forecast: PoolForecast) -> list[str]:
    lines: list[str] = []
    if forecast.remaining_percent is not None:
        remaining = f"{forecast.remaining_percent:.1f}%"
        if (
            forecast.available_percent is not None
            and forecast.available_percent != forecast.remaining_percent
        ):
            remaining += f"，可用{forecast.available_percent:.1f}%"
        lines.append(f"全池剩余{remaining}")
    if forecast.next_reset_at is not None:
        lines.append(f"最早重置：{_format_time(forecast.next_reset_at)}")
    return lines


def _format_forecast_scenarios(forecast: PoolForecast) -> list[str]:
    if not forecast.scenarios:
        return []
    lines = [_format_primary_scenario(forecast.scenarios[0], forecast)]
    if len(forecast.scenarios) > 1:
        lines.append(
            f"近6小时消耗：{forecast.scenarios[1].burn_percent_per_hour:.2f}%/小时"
        )
    return lines


def _format_primary_scenario(scenario: ForecastScenario, forecast: PoolForecast) -> str:
    rate = f"{scenario.burn_percent_per_hour:.2f}%/小时"
    prefix = f"近{scenario.lookback_hours}小时消耗：{rate}"
    if scenario.runway_hours is None:
        return f"{prefix}，暂无耗尽估计"
    if scenario.runway_hours <= 0:
        return f"{prefix}，当前可用额度已见底"
    if scenario.reaches_reset:
        return f"{prefix}，预计能撑到最早重置"
    exhaustion = forecast.generated_at + timedelta(hours=scenario.runway_hours)
    exhaustion_time = _format_time(exhaustion)
    result = (
        f"{prefix}，约剩{_format_duration(scenario.runway_hours)}"
        f"（预计{exhaustion_time}耗尽）"
    )
    if scenario.target_fraction is not None:
        result += f"；要撑到最早重置，需降至当前消耗的约{scenario.target_fraction:.0%}"
    return result


def _format_duration(hours: float) -> str:
    if hours < 1:
        return "不到1小时"
    if hours < _SHORT_RUNWAY_HOURS:
        return f"{hours:.0f}小时"
    return f"{hours / _HOURS_PER_DAY:.1f}天"


def _format_percent(value: float) -> str:
    return f"{value:g}%"


def _format_time(value: datetime) -> str:
    local = value.astimezone(_DISPLAY_TIMEZONE)
    return f"{local.month}月{local.day}日{local:%H:%M}"
