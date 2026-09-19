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
_PERIOD_LABELS = {
    "today": "今日",
    "yesterday": "昨日",
    "current_month": "本月",
    "previous_month": "上月",
}
_FORECAST_PROBLEMS = {
    "no_accounts": "暂无启用的 Codex 账号。",
    "missing_quota": "部分额度查询失败，暂不能估算全池续航。",
    "stale_quota": "额度数据已过期或正在重置，等待刷新后再查。",
    "mixed_plans": "订阅档位不一致或未知，暂不能合并估算额度。",
    "missing_routing": "调度状态不完整，暂不能估算可用额度。",
    "unstarted_window": "当前可调度账号暂无确定的自然回补时间，暂不预测续航。",
    "missing_history": "历史不足、过期或与当前周期不一致，暂不预测续航。",
}
_FORECAST_WARNINGS = {
    "routing_limited": "部分余额当前未参与调度，未计入续航；恢复后请重新查询。",
    "short_history": "历史未覆盖全部观察窗口，仅展示有足够数据的情景。",
    "subscription_expiring": "有订阅将在回补前到期，估算以按期续费为前提。",
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


def format_forecast(forecast: PoolForecast) -> str:
    lines = ["Codex 全池续航"]
    if forecast.remaining_percent is not None:
        lines.append(f"观测周额度剩余：{forecast.remaining_percent:.1f}%")
    if (
        forecast.available_percent is not None
        and forecast.available_percent != forecast.remaining_percent
    ):
        lines.append(f"其中可调度余额：{forecast.available_percent:.1f}%（按全池容量）")
    if forecast.available_percent == 0:
        lines.append("观测余额中没有可调度的周额度。")
    if forecast.next_reset_at is not None:
        lines.append(f"最近自然回补：{_format_time(forecast.next_reset_at)}")
    if forecast.problem is not None:
        lines.append(_FORECAST_PROBLEMS[forecast.problem])
    for scenario in forecast.scenarios:
        lines.extend(_format_forecast_scenario(forecast, scenario))
    lines.extend(_FORECAST_WARNINGS[warning] for warning in forecast.warnings)
    if forecast.observed_at is not None:
        lines.append(f"数据截至：{_format_time(forecast.observed_at)}")
    if forecast.scenarios:
        lines.append("已按各账号数据时间估算当前余额；不含短时限制及重置机会。")
    return "\n".join(lines)


def _format_forecast_scenario(
    forecast: PoolForecast, scenario: ForecastScenario
) -> list[str]:
    lines = [
        "",
        f"近{scenario.lookback_hours}小时："
        f"平均每小时消耗全池 {scenario.burn_percent_per_hour:.2f} 个百分点",
    ]
    if forecast.available_percent == 0:
        return lines
    if scenario.runway_hours is None:
        lines.append("未观察到可计量的额度扣减，暂不预测耗尽时间。")
    elif scenario.runway_hours <= 0:
        lines.append("按此节奏估算，当前可调度额度可能已经耗尽。")
    elif scenario.reaches_reset:
        lines.append("保持此节奏，预计可以撑到本次自然回补。")
    else:
        exhaustion = forecast.generated_at + timedelta(hours=scenario.runway_hours)
        local = exhaustion.astimezone(_DISPLAY_TIMEZONE)
        duration = (
            "不足1小时"
            if scenario.runway_hours < 1
            else f"约{scenario.runway_hours:.0f}小时"
        )
        lines.append(
            f"预计还能用{duration}，"
            f"约{local.month}月{local.day}日{local.hour}时耗尽，早于自然回补。"
        )
        if scenario.target_fraction is not None:
            lines.append(
                f"要撑到回补，全池总消耗需降至此节奏的约{scenario.target_fraction:.0%}。"
            )
    return lines


def _format_percent(value: float) -> str:
    return f"{value:g}%"


def _format_time(value: datetime) -> str:
    local = value.astimezone(_DISPLAY_TIMEZONE)
    return f"{local.month}月{local.day}日{local:%H:%M}"
