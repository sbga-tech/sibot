"""Compact Chinese wording shared by text messages and rendered images."""

from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .alerts import AlertEvent, ResetGrantChange
from .models import (
    PROVIDERS,
    AccountQuota,
    ClaudeResetGrant,
    CodexAccountResetCredits,
    ForecastScenario,
    PoolForecast,
    Provider,
    RankingMetric,
)
from .quota import is_login_invalid

DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
_HOURS_PER_DAY = 24
_SHORT_RUNWAY_HOURS = 48
_THOUSAND = 1_000
_MILLION = 1_000_000
PROVIDER_LABELS: dict[Provider, str] = {"codex": "Codex", "claude": "Claude"}
_UPSTREAM_NAMES: dict[Provider, str] = {"codex": "OpenAI", "claude": "Anthropic"}
PERIOD_LABELS = {
    "today": "今日",
    "yesterday": "昨日",
    "current_month": "本月",
    "previous_month": "上月",
}
RANKING_TITLES: dict[RankingMetric, str] = {
    "total_tokens": "Token 排名",
    "cost": "Cost 排名",
}
OTHER_MODELS_LABEL = "其他"
USER_SHARE_TITLE = "用户占比"
MODEL_SHARE_TITLE = "模型占比"
_FORECAST_PROBLEMS = {
    "no_accounts": "暂无账号",
    "missing_quota": "额度数据不完整",
    "stale_quota": "额度数据已过期",
    "unsupported_plans": "订阅额度无法换算",
    "missing_routing": "调度状态不完整",
    "unstarted_window": "暂无重置时间",
    "missing_history": "历史数据不足",
}
# Quota windows a Claude reset grant clears, in display order.
_GRANT_CLEARS = {"five_hour": "5h", "seven_day": "周"}
RESET_QUERY_FAILED = "查询失败"
NO_RESET_DATA = "暂无数据"
NO_RESET_GRANTS = "暂无重置机会"


def format_missing_grant_accounts(count: int) -> str:
    return f"{count} 个账号暂无数据"


def format_help() -> str:
    return "\n".join(
        (
            "可用指令：",
            "/ai rank [matcher] [today|yesterday|month|last-month] [tokens|cost]",
            "/ai quota",
            "/ai reset",
        )
    )


def format_ranking_value(metric: RankingMetric, value: int) -> str:
    """`value` is tokens, or micro-USD for cost."""
    if metric == "cost":
        return f"${value / _MILLION:,.2f}"
    return f"{value:,}"


def format_user_count(user_count: int) -> str:
    return f"{user_count} 人"


def format_other_users(user_count: int) -> str:
    return f"其余 {user_count} 人"


def format_share(share: float) -> str:
    """Share of a whole in [0, 1]; tiny non-zero shares stay visible as `<1%`."""
    percent = share * 100
    if 0 < percent < 1:
        return "<1%"
    return f"{percent:.0f}%"


def account_status(account: AccountQuota) -> str | None:
    """Short status label, or None when the account has usable quota."""
    if is_login_invalid(account):
        return "登录失效"
    if account.status == "unstarted":
        return "本周未使用"
    if account.status != "completed" or account.weekly is None:
        return "额度暂不可用"
    return None


def reset_credit_lines(account: CodexAccountResetCredits) -> tuple[str, list[str]]:
    """Headline status and expiry detail lines for one account's reset credits."""
    response = account.response
    if response is None:
        return RESET_QUERY_FAILED, []

    count = response.available_count
    status = "可用次数未知" if count is None else f"可用 {count} 次"
    if count == 0:
        return status, []

    available = [credit for credit in response.credits if credit.status == "available"]
    expiries = Counter(
        credit.expires_at.astimezone(DISPLAY_TIMEZONE)
        for credit in available
        if credit.expires_at is not None
    )
    details = [
        f"{amount} 次于 {format_short_time(expiry)} 过期"
        for expiry, amount in sorted(expiries.items())
    ]
    known_count = sum(expiries.values())
    if not expiries:
        details.append("过期时间未知")
    elif known_count < len(available) or (count is not None and known_count < count):
        details.append("部分过期时间未知")
    return status, details


def reset_grant_status(grant: ClaudeResetGrant, now: datetime) -> tuple[str, bool]:
    """Per-account state of one Claude grant, and whether it still holds resets."""
    if grant.ends_at is not None and grant.ends_at <= now:
        return "已过期", False
    if grant.resets_left == 0:
        return "已用完", False
    return f"剩 {grant.resets_left}/{grant.resets_total}", not grant.paused


def reset_grant_details(grant: ClaudeResetGrant, now: datetime) -> str:
    """Expiry and usage conditions shared by every account holding the grant."""
    parts: list[str] = []
    if grant.ends_at is not None:
        parts.append(f"{format_short_time(grant.ends_at)} 到期")
    windows = [label for key, label in _GRANT_CLEARS.items() if key in grant.clears]
    if windows:
        parts.append(f"清空 {'/'.join(windows)}额度")
    if grant.starts_at is not None and grant.starts_at > now:
        parts.append(f"{format_short_time(grant.starts_at)} 起可用")
    if grant.use_requires_limit:
        parts.append("额度用完后可用")
    if grant.paused:
        parts.append("已暂停")
    return " · ".join(parts)


def format_alert_batch(events: list[AlertEvent]) -> str:
    lines: list[str] = []
    for provider in PROVIDERS:
        group = [event for event in events if event.provider == provider]
        if group:
            lines.append(f"{PROVIDER_LABELS[provider]} 提醒")
            lines.extend(map(_format_alert, group))
    return "\n".join(lines)


def _format_alert(event: AlertEvent) -> str:
    if event.kind == "subscription_expiring":
        return (
            f"{event.display_name}：订阅将在{format_time(event.active_until)}到期，"
            f"剩余不超过{event.threshold_hours}小时，记得续费。"
        )
    if event.kind == "weekly_low":
        return (
            f"{event.display_name}：周额度只剩"
            f"{format_percent(event.remaining_percent)}，"
            f"{format_time(event.reset_at)}重置"
        )
    if event.kind == "weekly_exhausted":
        return f"{event.display_name}：周额度已用完，{format_time(event.reset_at)}重置"
    if event.kind == "weekly_reset":
        if event.source == "scheduled":
            action = "周额度已正常重置"
        elif event.source == "reset_credit":
            action = f"已使用重置机会{_grant_suffix(event.grants, with_expiry=False)}"
        else:
            action = f"{_UPSTREAM_NAMES[event.provider]}已重置周额度"
        return (
            f"{event.display_name}：{action}，"
            f"当前剩余{format_percent(event.remaining_percent)}，"
            f"下次{format_time(event.reset_at)}重置"
        )
    if event.kind == "reset_credit_increased":
        return (
            f"{event.display_name}：新增{event.added_count}次重置机会"
            f"{_grant_suffix(event.grants, with_expiry=True)}，"
            f"当前可用{event.available_count}次"
        )
    return f"{event.display_name}：登录已失效"


def _grant_suffix(changes: tuple[ResetGrantChange, ...], *, with_expiry: bool) -> str:
    if not changes:
        return ""
    texts = [
        f"{change.label}，{format_time(change.ends_at)}到期"
        if with_expiry and change.ends_at is not None
        else change.label
        for change in changes
    ]
    return f"（{'；'.join(texts)}）"


def forecast_lines(forecast: PoolForecast) -> list[str]:
    """Pool summary lines, most important first."""
    lines: list[str] = []
    if forecast.remaining_points is not None:
        remaining = f"池剩余 {forecast.remaining_points:.0f}%"
        if forecast.available_points is not None and round(
            forecast.available_points
        ) != round(forecast.remaining_points):
            remaining += f"，可用 {forecast.available_points:.0f}%"
        if forecast.unit_plan is not None:
            remaining += f"（{forecast.unit_plan}=100%）"
        lines.append(remaining)
    if forecast.estimated_tokens is not None:
        lines.append(f"约剩 {_format_tokens(forecast.estimated_tokens)} tokens")
    if forecast.next_reset_at is not None:
        lines.append(f"最早重置 {format_short_time(forecast.next_reset_at)}")
    if forecast.problem is not None:
        if forecast.problem not in {"unstarted_window", "missing_history"}:
            lines.append(_FORECAST_PROBLEMS[forecast.problem])
    elif forecast.scenario is not None:
        lines.append(_format_scenario(forecast.scenario, forecast))
    if "routing_limited" in forecast.warnings:
        lines.append("部分账号暂不可用")
    return lines


def _format_scenario(scenario: ForecastScenario, forecast: PoolForecast) -> str:
    rate = f"当前 {scenario.burn_points_per_hour:.1f}%/h"
    if scenario.runway_hours is None:
        return rate
    if scenario.runway_hours <= 0:
        return f"{rate}，额度已见底"
    if scenario.reaches_reset:
        return f"{rate}，能撑到重置"
    exhaustion = forecast.generated_at + timedelta(hours=scenario.runway_hours)
    return (
        f"{rate}，约{_format_duration(scenario.runway_hours)}后"
        f"（{format_time(exhaustion)}）耗尽"
    )


def _format_tokens(value: float) -> str:
    if value < _THOUSAND:
        return f"{value:.0f}"
    if value < _MILLION:
        return f"{value / _THOUSAND:.1f}K"
    return f"{value / _MILLION:.1f}M"


def _format_duration(hours: float) -> str:
    if hours < 1:
        return "不到1小时"
    if hours < _SHORT_RUNWAY_HOURS:
        return f"{hours:.0f}小时"
    return f"{hours / _HOURS_PER_DAY:.1f}天"


def format_percent(value: float) -> str:
    return f"{value:.0f}%"


def format_time(value: datetime) -> str:
    local = value.astimezone(DISPLAY_TIMEZONE)
    return f"{local.month}月{local.day}日{local:%H:%M}"


def format_short_time(value: datetime) -> str:
    local = value.astimezone(DISPLAY_TIMEZONE)
    return f"{local:%m-%d %H:%M}"
