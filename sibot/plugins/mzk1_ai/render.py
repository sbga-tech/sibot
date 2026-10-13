"""PNG cards for `/ai quota`, `/ai rank`, and `/ai reset`."""

import io
import zlib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import TypeAlias

from PIL import Image, ImageDraw, ImageFont

from .formatting import (
    MODEL_SHARE_TITLE,
    NO_RESET_DATA,
    NO_RESET_GRANTS,
    OTHER_MODELS_LABEL,
    PERIOD_LABELS,
    PROVIDER_LABELS,
    RANKING_TITLES,
    RESET_QUERY_FAILED,
    USER_SHARE_TITLE,
    account_status,
    forecast_lines,
    format_missing_grant_accounts,
    format_other_users,
    format_percent,
    format_ranking_value,
    format_share,
    format_short_time,
    format_user_count,
    reset_credit_lines,
    reset_grant_details,
    reset_grant_status,
)
from .models import (
    PROVIDERS,
    AccountQuota,
    ClaudeResetGrant,
    CodexAccountResetCredits,
    PoolForecast,
    Provider,
    RankingEntry,
    RankingMetric,
    RankingModel,
    RankingResponse,
    ResetOverview,
    WindowQuota,
)

# Drawing happens at 2x and is sent as-is; QQ scales it to the screen.
_SCALE = 2
_WIDTH = 360 * _SCALE
_PAD = 16 * _SCALE
_GAP = 10 * _SCALE

# Light theme with a pale blue tint.
_BG = (238, 245, 252)
_PANEL = (255, 255, 255)
_LINE = (218, 229, 241)
_TRACK = (226, 235, 245)
_FG = (30, 41, 59)
_MUTED = (100, 116, 139)
_ACCENT = (37, 99, 235)
_GREEN = (22, 163, 74)
_YELLOW = (217, 119, 6)
_RED = (220, 38, 38)
_AVATAR_BG = (219, 234, 254)
_AVATAR_COLORS = (_ACCENT, _GREEN, _YELLOW, _RED, (124, 58, 237), (8, 145, 178))

# Maple Mono Normal NL NF CN, copied into the image by the Dockerfile.
_FONT_DIR = Path("/usr/share/fonts/maple")
_FONT_PATH = _FONT_DIR / "MapleMonoNormalNL-NF-CN-Regular.ttf"
_BOLD_FONT_PATH = _FONT_DIR / "MapleMonoNormalNL-NF-CN-Bold.ttf"
_LOW_PERCENT = 50
_EMPTY_PERCENT = 10
_FULL_PERCENT = 100.0


@cache
def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        str(_BOLD_FONT_PATH if bold else _FONT_PATH), size * _SCALE
    )


@dataclass(frozen=True, slots=True)
class _Text:
    text: str
    size: int = 13
    color: tuple[int, int, int] = _FG
    bold: bool = False

    @property
    def font(self) -> ImageFont.FreeTypeFont:
        return _font(self.size, bold=self.bold)


_Draw: TypeAlias = Callable[[Image.Image, ImageDraw.ImageDraw], None]


class _Canvas:
    """Top-to-bottom layout on an image that grows as content is added."""

    def __init__(self, width: int = _WIDTH) -> None:
        self._ops: list[_Draw] = []
        self.y = _PAD
        self.width = width

    def text(self, x: int, item: _Text, *, right: bool = False) -> int:
        width = self.measure(item)
        position = (x - width if right else x, self.y)
        self._ops.append(
            lambda _, draw: draw.text(
                position, item.text, font=item.font, fill=item.color
            )
        )
        return width

    def rect(
        self, box: tuple[int, int, int, int], color: tuple[int, int, int], radius: int
    ) -> None:
        self._ops.append(
            lambda _, draw: draw.rounded_rectangle(box, radius=radius, fill=color)
        )

    def paste(self, source: Image.Image, x: int, y: int) -> None:
        self._ops.append(lambda image, _: image.paste(source, (x, y), source))

    def open_panel(self) -> int:
        """Start a panel at the current height; returns a handle for close_panel."""
        return len(self._ops)

    def close_panel(self, handle: int, top: int) -> None:
        """Draw a panel from `top` to the current height beneath its contents."""
        box = (_PAD, top, self.width - _PAD, self.y)
        self._ops.insert(
            handle,
            lambda _, draw: draw.rounded_rectangle(box, radius=8 * _SCALE, fill=_PANEL),
        )

    @staticmethod
    def measure(item: _Text) -> int:
        return int(item.font.getlength(item.text))

    @staticmethod
    def line_height(size: int) -> int:
        return int(size * _SCALE * 1.45)

    def render(self) -> bytes:
        image = Image.new("RGB", (self.width, self.y + _PAD), _BG)
        draw = ImageDraw.Draw(image)
        for operation in self._ops:
            operation(image, draw)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def _header(canvas: _Canvas, title: str, subtitle: str) -> None:
    subtitle_width = canvas.text(
        canvas.width - _PAD, _Text(subtitle, size=11, color=_MUTED), right=True
    )
    title_width = canvas.width - 2 * _PAD - subtitle_width - _GAP
    canvas.text(_PAD, _truncate(_Text(title, size=17, bold=True), title_width))
    canvas.y += canvas.line_height(17) + _GAP


def _percent_color(remaining: float) -> tuple[int, int, int]:
    if remaining <= _EMPTY_PERCENT:
        return _RED
    if remaining <= _LOW_PERCENT:
        return _YELLOW
    return _GREEN


def _bar(canvas: _Canvas, x: int, width: int, remaining: float) -> None:
    height = 4 * _SCALE
    top = canvas.y + canvas.line_height(12) // 2 - height // 2
    canvas.rect((x, top, x + width, top + height), _TRACK, height // 2)
    filled = int(width * max(0.0, min(remaining, 100.0)) / 100)
    if filled > 0:
        canvas.rect(
            (x, top, x + filled, top + height), _percent_color(remaining), height // 2
        )


# ---------------------------------------------------------------- quota


def render_quota(
    accounts: Sequence[AccountQuota],
    forecasts: Mapping[Provider, PoolForecast],
    window_activity: Mapping[str, bool | None],
    generated_at: datetime,
) -> bytes:
    canvas = _Canvas()
    _header(canvas, "额度", format_short_time(generated_at))
    for provider in PROVIDERS:
        group = [account for account in accounts if account.provider == provider]
        if group:
            _quota_section(
                canvas, provider, group, forecasts.get(provider), window_activity
            )
    if not accounts:
        canvas.text(_PAD, _Text("暂无可用账号", color=_MUTED))
        canvas.y += canvas.line_height(13)
    return canvas.render()


def _quota_section(
    canvas: _Canvas,
    provider: Provider,
    accounts: Sequence[AccountQuota],
    forecast: PoolForecast | None,
    window_activity: Mapping[str, bool | None],
) -> None:
    top = canvas.y
    panel = canvas.open_panel()
    canvas.y += _GAP
    inner = _PAD + _GAP
    canvas.text(
        inner, _Text(PROVIDER_LABELS[provider], size=15, bold=True, color=_ACCENT)
    )
    canvas.y += canvas.line_height(15)
    if forecast is not None:
        for line in forecast_lines(forecast):
            canvas.text(inner, _Text(line, size=11, color=_MUTED))
            canvas.y += canvas.line_height(11)
    for account in accounts:
        canvas.y += _GAP // 2
        canvas.rect((inner, canvas.y, _WIDTH - inner, canvas.y + 1), _LINE, 0)
        canvas.y += _GAP // 2 + 2 * _SCALE
        _account_row(
            canvas, account, window_active=window_activity.get(account.credential_id)
        )
    canvas.y += _GAP
    canvas.close_panel(panel, top)
    canvas.y += _GAP


_LABEL_WIDTH = 30 * _SCALE
_PERCENT_WIDTH = 40 * _SCALE


def _account_row(
    canvas: _Canvas, account: AccountQuota, *, window_active: bool | None
) -> None:
    inner = _PAD + _GAP
    right = _WIDTH - _PAD - _GAP
    plan_width = 0
    if account.plan:
        plan = _Text(account.plan, size=11, color=_MUTED)
        plan_width = canvas.measure(plan) + _GAP
        canvas.text(right, plan, right=True)
    canvas.text(
        inner,
        _truncate(_Text(account.display_name, size=13), right - inner - plan_width),
    )
    canvas.y += canvas.line_height(13)

    status = account_status(account)
    if status is not None:
        color = _MUTED if account.status == "unstarted" else _RED
        canvas.text(inner, _Text(status, size=12, color=color))
        canvas.y += canvas.line_height(12)
        return
    windows = (
        (("5h", account.five_hour), ("周", account.weekly))
        if account.five_hour is not None
        else (("周", account.weekly),)
    )
    # Side by side, sharing the row width.
    cell = (right - inner - _GAP * (len(windows) - 1)) // len(windows)
    for index, (label, window) in enumerate(windows):
        _window_cell(canvas, inner + index * (cell + _GAP), cell, label, window)
    canvas.y += canvas.line_height(12)

    details: list[_Text] = []
    weekly = account.weekly
    # An unstarted window reports a rolling reset time that moves with every
    # refresh; only show it once the window has started.
    if weekly is not None and (
        weekly.exhausted
        or weekly.remaining_percent < _FULL_PERCENT
        or window_active is True
    ):
        details.append(_muted(f"周重置 {format_short_time(weekly.reset_at)}"))
    until = account.subscription_active_until
    # Only an expiry that lands before this window's reset changes the picture.
    if until is not None and weekly is not None and until <= weekly.reset_at:
        details.append(
            _Text(f"订阅 {format_short_time(until)} 到期", size=10, color=_YELLOW)
        )
    details.extend(
        _muted(f"{model.label} 剩{format_percent(model.remaining_percent)}")
        for model in account.models
    )
    if account.extra_usage_percent is not None:
        details.append(
            _muted(f"额外用量 {format_percent(account.extra_usage_percent)}")
        )
    if details:
        x = inner
        for index, item in enumerate(details):
            if index:
                x += canvas.text(x, _muted(" · "))
            x += canvas.text(x, item)
        canvas.y += canvas.line_height(10)


def _muted(text: str) -> _Text:
    return _Text(text, size=10, color=_MUTED)


def _window_cell(
    canvas: _Canvas, x: int, width: int, label: str, window: WindowQuota | None
) -> None:
    canvas.text(x, _Text(label, size=11, color=_MUTED))
    percent_right = x + width
    if window is None:
        canvas.text(percent_right, _Text("-", size=12, color=_MUTED), right=True)
        return
    remaining = 0.0 if window.exhausted else window.remaining_percent
    canvas.text(
        percent_right,
        _Text(
            format_percent(remaining),
            size=12,
            color=_percent_color(remaining),
        ),
        right=True,
    )
    bar_x = x + _LABEL_WIDTH
    _bar(canvas, bar_x, percent_right - _PERCENT_WIDTH - bar_x, remaining)


def _truncate(item: _Text, width: int) -> _Text:
    if _Canvas.measure(item) <= width:
        return item
    text = item.text
    while text and _Canvas.measure(replace(item, text=f"{text}…")) > width:
        text = text[:-1]
    return replace(item, text=f"{text}…")


# ---------------------------------------------------------------- reset


def render_reset_credits(overview: ResetOverview, generated_at: datetime) -> bytes:
    canvas = _Canvas()
    _header(canvas, "重置机会", format_short_time(generated_at))
    if overview.codex != ():
        _codex_reset_section(canvas, overview.codex)
    if overview.claude != ():
        _claude_reset_section(canvas, overview.claude, generated_at)
    if overview.codex == () and overview.claude == ():
        canvas.text(_PAD, _Text("暂无可用账号", color=_MUTED))
        canvas.y += canvas.line_height(13)
    return canvas.render()


def _reset_section_title(canvas: _Canvas, provider: Provider) -> None:
    canvas.y += _GAP
    canvas.text(
        _PAD + _GAP,
        _Text(PROVIDER_LABELS[provider], size=15, bold=True, color=_ACCENT),
    )
    canvas.y += canvas.line_height(15)


def _reset_separator(canvas: _Canvas) -> None:
    canvas.y += _GAP // 2
    canvas.rect((_PAD + _GAP, canvas.y, _WIDTH - _PAD - _GAP, canvas.y + 1), _LINE, 0)
    canvas.y += _GAP // 2 + 2 * _SCALE


def _codex_reset_section(
    canvas: _Canvas, accounts: Sequence[CodexAccountResetCredits] | None
) -> None:
    top = canvas.y
    panel = canvas.open_panel()
    _reset_section_title(canvas, "codex")
    inner = _PAD + _GAP
    right = _WIDTH - _PAD - _GAP
    if accounts is None:
        canvas.text(inner, _Text(RESET_QUERY_FAILED, size=12, color=_RED))
        canvas.y += canvas.line_height(12)
    for account in accounts or ():
        _reset_separator(canvas)
        status, details = reset_credit_lines(account)
        count = account.response.available_count if account.response else None
        color = _MUTED if count == 0 else _RED if account.response is None else _ACCENT
        status_text = _Text(status, size=12, bold=True, color=color)
        status_width = canvas.measure(status_text)
        canvas.text(right, status_text, right=True)
        canvas.text(
            inner,
            _truncate(
                _Text(account.display_name, size=13),
                right - inner - status_width - _GAP,
            ),
        )
        canvas.y += canvas.line_height(13)
        for line in details:
            canvas.text(inner, _Text(line, size=10, color=_MUTED))
            canvas.y += canvas.line_height(10)
    canvas.y += _GAP
    canvas.close_panel(panel, top)
    canvas.y += _GAP


def _claude_reset_section(
    canvas: _Canvas, accounts: Sequence[AccountQuota] | None, now: datetime
) -> None:
    top = canvas.y
    panel = canvas.open_panel()
    _reset_section_title(canvas, "claude")
    inner = _PAD + _GAP
    groups = _grant_groups(accounts or ())
    notice = _claude_reset_notice(accounts, has_grants=bool(groups))
    if notice is not None:
        canvas.text(inner, notice)
        canvas.y += canvas.line_height(12)
    for grant, holders in groups:
        _reset_separator(canvas)
        _grant_block(canvas, grant, holders, now)
    if accounts is not None and groups:
        missing = sum(account.claude_reset_grants is None for account in accounts)
        if missing:
            _reset_separator(canvas)
            canvas.text(
                inner,
                _Text(format_missing_grant_accounts(missing), size=10, color=_MUTED),
            )
            canvas.y += canvas.line_height(10)
    canvas.y += _GAP
    canvas.close_panel(panel, top)
    canvas.y += _GAP


def _claude_reset_notice(
    accounts: Sequence[AccountQuota] | None, *, has_grants: bool
) -> _Text | None:
    if accounts is None:
        return _Text(RESET_QUERY_FAILED, size=12, color=_RED)
    if has_grants:
        return None
    if all(account.claude_reset_grants is None for account in accounts):
        return _Text(NO_RESET_DATA, size=12, color=_MUTED)
    return _Text(NO_RESET_GRANTS, size=12, color=_MUTED)


def _grant_block(
    canvas: _Canvas,
    grant: ClaudeResetGrant,
    holders: Sequence[tuple[str, ClaudeResetGrant]],
    now: datetime,
) -> None:
    inner = _PAD + _GAP
    right = _WIDTH - _PAD - _GAP
    title = _Text(grant.label or grant.grant_id, size=12, bold=True)
    for line in _wrap(title, right - inner):
        canvas.text(inner, line)
        canvas.y += canvas.line_height(12)
    details = reset_grant_details(grant, now)
    if details:
        for line in _wrap(_Text(details, size=10, color=_MUTED), right - inner):
            canvas.text(inner, line)
            canvas.y += canvas.line_height(10)
    canvas.y += 2 * _SCALE
    for display_name, held in holders:
        text, available = reset_grant_status(held, now)
        status = _Text(
            text,
            size=12,
            bold=available,
            color=_ACCENT if available else _MUTED,
        )
        status_width = canvas.text(right, status, right=True)
        canvas.text(
            inner,
            _truncate(
                _Text(display_name, size=12), right - inner - status_width - _GAP
            ),
        )
        canvas.y += canvas.line_height(12)


def _grant_groups(
    accounts: Sequence[AccountQuota],
) -> list[tuple[ClaudeResetGrant, list[tuple[str, ClaudeResetGrant]]]]:
    """Group each account's copy of a grant under the grant, soonest expiry first."""
    holders: dict[str, list[tuple[str, ClaudeResetGrant]]] = {}
    for account in accounts:
        for grant in account.claude_reset_grants or ():
            holders.setdefault(grant.grant_id, []).append((account.display_name, grant))
    groups = [(members[0][1], members) for members in holders.values()]
    groups.sort(
        key=lambda group: (
            group[0].ends_at is None,
            group[0].ends_at.timestamp() if group[0].ends_at else 0.0,
            group[0].grant_id,
        )
    )
    return groups


def _wrap(item: _Text, width: int) -> list[_Text]:
    """Break text at spaces to fit `width`; an overlong word is truncated."""
    lines: list[str] = []
    line = ""
    for word in item.text.split(" "):
        candidate = f"{line} {word}" if line else word
        if line and _Canvas.measure(replace(item, text=candidate)) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    return [_truncate(replace(item, text=text), width) for text in lines]


# ---------------------------------------------------------------- rank

_RGB: TypeAlias = tuple[int, int, int]
_RANK_WIDTH = 480 * _SCALE
_AVATAR_SIZE = 32 * _SCALE
_ROW_BAR_HEIGHT = 6 * _SCALE
_TOTAL_BAR_HEIGHT = 10 * _SCALE
_SWATCH_SIZE = 8 * _SCALE
_LEGEND_COLUMNS = 3
_LEGEND_COLUMN_GAP = 16 * _SCALE
_LEGEND_INNER_GAP = 4 * _SCALE
_LEGEND_FONT_SIZE = 9
# User share donut: the largest users get their own slice, the rest merge.
_USER_SHARE_TOP = 9
_USER_SHARE_COLUMNS = 2
_DONUT_SIZE = 112 * _SCALE
_DONUT_HOLE = 0.58
_DONUT_SUPERSAMPLE = 4
_USER_SHARE_AVATAR_SIZE = 14 * _SCALE
_USER_SHARE_ROW_GAP = 6 * _SCALE
_USER_SHARE_RANGE = (-0.4, 0.6)
# Models below this share of the card total are drawn as one "other" slice.
_MIN_MODEL_SHARE = 0.01
_OTHER_COLOR = (203, 213, 225)
# Warm grey so its light shades stay apart from the cool "other" slice.
_UNKNOWN_VENDOR_COLOR = (120, 113, 108)
# Model-name prefix -> vendor hue. Hues follow the vendor's brand colour unless
# it would be confused with another vendor's.
_VENDOR_COLORS: tuple[tuple[tuple[str, ...], _RGB], ...] = (
    (("claude",), (217, 119, 87)),  # Anthropic clay
    (("gpt", "codex", "chatgpt"), (16, 163, 127)),  # OpenAI, ChatGPT green
    (("gemini", "gemma"), (66, 133, 244)),  # Google blue
    (("grok",), (51, 65, 85)),  # xAI black
    (("deepseek",), (79, 70, 229)),  # DeepSeek blue, shifted to indigo
    (("glm",), (8, 145, 178)),  # Zhipu blue, shifted to cyan
    (("qwen",), (147, 51, 234)),  # Qwen purple
    (("kimi",), (219, 39, 119)),  # Moonshot is monochrome; pink
    (("mimo",), (234, 179, 8)),  # Xiaomi orange, shifted to amber
    (("muse",), (101, 163, 13)),  # lime
)
# Shades within one vendor: mix toward white (+) or black (-).
_SHADES = (0.0, 0.35, -0.3, 0.6, -0.5)
_SHADE_RANGE = (-0.55, 0.65)
_TOP_COLORS = {1: (202, 138, 4), 2: (100, 116, 139), 3: (180, 83, 9)}


@dataclass(frozen=True, slots=True)
class _Legend:
    """Models drawn individually on one card, in legend order."""

    models: tuple[str, ...]
    colors: Mapping[str, _RGB]
    totals: Mapping[str, int]
    other: int

    @property
    def total(self) -> int:
        return sum(self.totals.values()) + self.other

    def slices(self, models: Sequence[RankingModel]) -> list[tuple[_RGB, int]]:
        values = {model.model: model.value for model in models}
        slices = [
            (self.colors[name], values[name])
            for name in self.models
            if values.get(name, 0) > 0
        ]
        other = sum(value for name, value in values.items() if name not in self.colors)
        if other > 0:
            slices.append((_OTHER_COLOR, other))
        return slices


def render_ranking(
    ranking: RankingResponse, avatars: Sequence[Image.Image | None]
) -> bytes:
    canvas = _Canvas(_RANK_WIDTH)
    subtitle = format_short_time(ranking.generated_at)
    if ranking.stale:
        subtitle += " · 可能已过期"
    title = f"{RANKING_TITLES[ranking.metric]} · {PERIOD_LABELS[ranking.period]}"
    if ranking.matcher:
        title += f" · {ranking.matcher}"
    _header(canvas, title, subtitle)
    if not ranking.entries:
        canvas.text(_PAD, _Text("暂无数据", color=_MUTED))
        canvas.y += canvas.line_height(13)
        return canvas.render()

    # Users first; the model legend sits directly above the rows it colours.
    legend = _legend(ranking.entries)
    _user_share(canvas, ranking, avatars)
    canvas.y += _GAP
    _model_share(canvas, legend)
    canvas.y += _GAP
    top = canvas.y
    panel = canvas.open_panel()
    canvas.y += _GAP
    for entry, avatar in zip(ranking.entries, avatars, strict=True):
        _ranking_row(canvas, entry, avatar, metric=ranking.metric, legend=legend)
    canvas.close_panel(panel, top)
    return canvas.render()


def _legend(entries: Sequence[RankingEntry]) -> _Legend:
    totals: Counter[str] = Counter()
    for entry in entries:
        for model in entry.models:
            totals[model.model] += model.value
    grand = sum(totals.values())
    shown = {
        model: value
        for model, value in totals.items()
        if value > 0 and value >= grand * _MIN_MODEL_SHARE
    }
    vendors: dict[_RGB, list[str]] = {}
    for model in sorted(shown, key=lambda name: (-shown[name], name)):
        vendors.setdefault(_vendor_color(model), []).append(model)
    order: list[str] = []
    colors: dict[str, _RGB] = {}
    for base, models in sorted(
        vendors.items(), key=lambda item: -sum(shown[name] for name in item[1])
    ):
        for model, amount in zip(models, _vendor_shades(models), strict=True):
            colors[model] = _shade(base, amount)
            order.append(model)
    return _Legend(tuple(order), colors, shown, grand - sum(shown.values()))


def _vendor_color(model: str) -> _RGB:
    name = model.rsplit("/", 1)[-1].lower()
    for prefixes, color in _VENDOR_COLORS:
        if name.startswith(prefixes):
            return color
    return _UNKNOWN_VENDOR_COLOR


def _vendor_shades(models: Sequence[str]) -> list[float]:
    """Shade amounts for one vendor's models on a card, largest model first.

    Up to len(_SHADES) models keep a stable per-model shade (a collision takes
    the next free one, so the bigger model keeps its shade). Larger groups get
    evenly spaced shades, closest to the base colour for the biggest models,
    so no two models on a card share a colour.
    """
    if len(models) > len(_SHADES):
        low, high = _SHADE_RANGE
        step = (high - low) / (len(models) - 1)
        return sorted((low + index * step for index in range(len(models))), key=abs)
    taken: list[int] = []
    for model in models:
        preferred = zlib.crc32(model.encode()) % len(_SHADES)
        taken.append(
            next(
                (preferred + offset) % len(_SHADES)
                for offset in range(len(_SHADES))
                if (preferred + offset) % len(_SHADES) not in taken
            )
        )
    return [_SHADES[index] for index in taken]


def _shade(color: _RGB, amount: float) -> _RGB:
    target = 255 if amount > 0 else 0
    red, green, blue = (round(c + (target - c) * abs(amount)) for c in color)
    return (red, green, blue)


def _stacked_bar(
    canvas: _Canvas, x: int, width: int, height: int, slices: Sequence[tuple[_RGB, int]]
) -> None:
    """Draw at the current height; an unknown composition stays a plain track."""
    width = max(width, height)
    bar = Image.new("RGBA", (width, height), (*_TRACK, 255))
    draw = ImageDraw.Draw(bar)
    total = sum(value for _, value in slices)
    start = cumulative = 0
    for color, value in slices:
        cumulative += value
        end = round(width * cumulative / total)
        if end > start:
            draw.rectangle((start, 0, end - 1, height - 1), fill=color)
        start = end
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=height // 2, fill=255
    )
    bar.putalpha(mask)
    canvas.paste(bar, x, canvas.y)


def _model_share(canvas: _Canvas, legend: _Legend) -> None:
    top = canvas.y
    panel = canvas.open_panel()
    canvas.y += _GAP
    inner = _PAD + _GAP
    right = canvas.width - inner
    canvas.text(inner, _Text(MODEL_SHARE_TITLE, size=12, bold=True))
    canvas.y += canvas.line_height(12)

    slices = [(legend.colors[name], legend.totals[name]) for name in legend.models]
    if legend.other > 0:
        slices.append((_OTHER_COLOR, legend.other))
    _stacked_bar(canvas, inner, right - inner, _TOTAL_BAR_HEIGHT, slices)
    canvas.y += _TOTAL_BAR_HEIGHT + _GAP

    items = [(name, legend.colors[name], legend.totals[name]) for name in legend.models]
    if legend.other > 0:
        items.append((OTHER_MODELS_LABEL, _OTHER_COLOR, legend.other))
    _legend_items(canvas, inner, right, items, legend.total)
    canvas.y += _GAP // 2
    canvas.close_panel(panel, top)


def _legend_items(
    canvas: _Canvas,
    left: int,
    right: int,
    items: Sequence[tuple[str, _RGB, int]],
    total: int,
) -> None:
    """Lay out `swatch name share` items in a full-width, column-major grid.

    The grid always spans the panel: equal columns separated by a fixed gap,
    shares right-aligned at each column's end so the last column lines up with
    the total above. Names that do not fit their column are truncated.
    """
    size = _LEGEND_FONT_SIZE
    line = canvas.line_height(size)
    column_width = (
        right - left - (_LEGEND_COLUMNS - 1) * _LEGEND_COLUMN_GAP
    ) // _LEGEND_COLUMNS
    rows = -(-len(items) // _LEGEND_COLUMNS)
    top = canvas.y
    for index, (label, color, value) in enumerate(items):
        x = left + index // rows * (column_width + _LEGEND_COLUMN_GAP)
        canvas.y = top + index % rows * line
        swatch_top = canvas.y + (line - _SWATCH_SIZE) // 2 - _SCALE
        canvas.rect(
            (x, swatch_top, x + _SWATCH_SIZE, swatch_top + _SWATCH_SIZE),
            color,
            2 * _SCALE,
        )
        share = _Text(format_share(value / total), size=size, color=_MUTED)
        share_width = canvas.text(x + column_width, share, right=True)
        name_x = x + _SWATCH_SIZE + _LEGEND_INNER_GAP
        name_width = x + column_width - share_width - _LEGEND_INNER_GAP - name_x
        canvas.text(name_x, _truncate(_Text(label, size=size), name_width))
    canvas.y = top + rows * line


def _user_share(
    canvas: _Canvas,
    ranking: RankingResponse,
    avatars: Sequence[Image.Image | None],
) -> None:
    """Card total and a donut of each user's share of it, largest users first."""
    entries = ranking.entries
    total = sum(entry.value for entry in entries)
    if total <= 0:
        return
    top = canvas.y
    panel = canvas.open_panel()
    canvas.y += _GAP
    inner = _PAD + _GAP
    right = canvas.width - inner
    canvas.text(inner, _Text(USER_SHARE_TITLE, size=12, bold=True))
    total_text = _Text(format_ranking_value(ranking.metric, total), size=12, bold=True)
    canvas.text(right, total_text, right=True)
    canvas.y += canvas.line_height(12)

    shown = list(zip(entries, avatars, strict=True))
    rest: list[RankingEntry] = []
    if len(shown) > _USER_SHARE_TOP + 1:
        rest = [entry for entry, _ in shown[_USER_SHARE_TOP:]]
        shown = shown[:_USER_SHARE_TOP]
    low, high = _USER_SHARE_RANGE
    step = (high - low) / (len(shown) - 1) if len(shown) > 1 else 0.0
    colors = [_shade(_ACCENT, low + index * step) for index in range(len(shown))]
    slices = [
        (color, entry.value) for color, (entry, _) in zip(colors, shown, strict=True)
    ]
    rest_value = sum(entry.value for entry in rest)
    if rest_value > 0:
        slices.append((_OTHER_COLOR, rest_value))
    body_top = canvas.y
    canvas.paste(_donut(slices), inner, body_top)
    count = _Text(format_user_count(len(entries)), size=12, bold=True)
    canvas.y = body_top + (_DONUT_SIZE - canvas.line_height(12)) // 2
    canvas.text(inner + (_DONUT_SIZE - canvas.measure(count)) // 2, count)

    items: list[tuple[_RGB, Image.Image | None, str, int]] = [
        (
            color,
            _avatar(avatar, entry.user.github_login, _USER_SHARE_AVATAR_SIZE),
            entry.user.github_login,
            entry.value,
        )
        for color, (entry, avatar) in zip(colors, shown, strict=True)
    ]
    if rest:
        items.append((_OTHER_COLOR, None, format_other_users(len(rest)), rest_value))
    left = inner + _DONUT_SIZE + 2 * _GAP
    bottom = _user_share_legend(
        canvas, items, left=left, right=right, top=body_top, total=total
    )
    canvas.y = max(bottom, body_top + _DONUT_SIZE) + _GAP
    canvas.close_panel(panel, top)


def _user_share_legend(  # noqa: PLR0913
    canvas: _Canvas,
    items: Sequence[tuple[_RGB, Image.Image | None, str, int]],
    *,
    left: int,
    right: int,
    top: int,
    total: int,
) -> int:
    """Two aligned columns beside the donut, centred on it; returns the bottom."""
    line = canvas.line_height(10) + _USER_SHARE_ROW_GAP
    rows = -(-len(items) // _USER_SHARE_COLUMNS)
    column_width = (
        right - left - (_USER_SHARE_COLUMNS - 1) * _LEGEND_COLUMN_GAP
    ) // _USER_SHARE_COLUMNS
    grid_top = top + max(0, (_DONUT_SIZE - rows * line) // 2)
    for index, (color, avatar, label, value) in enumerate(items):
        x = left + index // rows * (column_width + _LEGEND_COLUMN_GAP)
        row_top = grid_top + index % rows * line
        swatch_top = row_top + (line - _SWATCH_SIZE) // 2 - _SCALE
        canvas.rect(
            (x, swatch_top, x + _SWATCH_SIZE, swatch_top + _SWATCH_SIZE),
            color,
            2 * _SCALE,
        )
        name_x = x + _SWATCH_SIZE + _LEGEND_INNER_GAP
        if avatar is not None:
            canvas.paste(avatar, name_x, row_top + (line - avatar.height) // 2 - _SCALE)
            name_x += avatar.width + _LEGEND_INNER_GAP
        canvas.y = row_top + _USER_SHARE_ROW_GAP // 2
        share = _Text(format_share(value / total), size=10, color=_MUTED)
        share_width = canvas.text(x + column_width, share, right=True)
        name_width = x + column_width - share_width - _LEGEND_INNER_GAP - name_x
        canvas.text(name_x, _truncate(_Text(label, size=10), name_width))
    return grid_top + rows * line


def _donut(slices: Sequence[tuple[_RGB, int]]) -> Image.Image:
    """Anti-aliased donut, drawn large and downsampled; slices start at 12 o'clock."""
    size = _DONUT_SIZE * _DONUT_SUPERSAMPLE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    total = sum(value for _, value in slices)
    start = cumulative = 0.0
    separator = _SCALE * _DONUT_SUPERSAMPLE
    for color, value in slices:
        cumulative += value
        end = 360 * cumulative / total
        if end > start:
            draw.pieslice(
                (0, 0, size - 1, size - 1),
                start - 90,
                end - 90,
                fill=color,
                outline=_PANEL if len(slices) > 1 else None,
                width=separator,
            )
        start = end
    hole = round(size * (1 - _DONUT_HOLE) / 2)
    draw.ellipse((hole, hole, size - 1 - hole, size - 1 - hole), fill=(*_PANEL, 255))
    return image.resize((_DONUT_SIZE, _DONUT_SIZE), Image.Resampling.LANCZOS)


def _ranking_row(
    canvas: _Canvas,
    entry: RankingEntry,
    avatar: Image.Image | None,
    *,
    metric: RankingMetric,
    legend: _Legend,
) -> None:
    rank = entry.rank
    login = entry.user.github_login
    name = entry.user.github_name
    inner = _PAD + _GAP
    right = canvas.width - _PAD - _GAP
    row_top = canvas.y
    rank_text = _Text(
        str(rank), size=14, bold=True, color=_TOP_COLORS.get(rank, _MUTED)
    )
    canvas.y = row_top + (_AVATAR_SIZE - canvas.line_height(14)) // 2
    canvas.text(inner + 18 * _SCALE, rank_text, right=True)
    avatar_x = inner + 26 * _SCALE
    canvas.paste(_avatar(avatar, login), avatar_x, row_top)

    text_x = avatar_x + _AVATAR_SIZE + _GAP
    value_text = _Text(format_ranking_value(metric, entry.value), size=13)
    canvas.y = row_top + (_AVATAR_SIZE - canvas.line_height(13)) // 2
    value_width = canvas.text(right, value_text, right=True)

    name_width = right - value_width - _GAP - text_x
    has_name = bool(name.strip()) and name.strip() != login
    if has_name:
        canvas.y = row_top - 2 * _SCALE
        canvas.text(text_x, _truncate(_Text(login, size=13, bold=True), name_width))
        canvas.y = row_top + canvas.line_height(13) - 4 * _SCALE
        canvas.text(
            text_x, _truncate(_Text(name.strip(), size=10, color=_MUTED), name_width)
        )
    else:
        canvas.y = row_top + (_AVATAR_SIZE - canvas.line_height(13)) // 2
        canvas.text(text_x, _truncate(_Text(login, size=13, bold=True), name_width))

    # Rows show each user's own model mix; the donut above compares users.
    canvas.y = row_top + _AVATAR_SIZE + 2 * _SCALE
    _stacked_bar(
        canvas, text_x, right - text_x, _ROW_BAR_HEIGHT, legend.slices(entry.models)
    )
    canvas.y += _ROW_BAR_HEIGHT + _GAP


def _avatar(
    image: Image.Image | None, login: str, size: int = _AVATAR_SIZE
) -> Image.Image:
    radius = size // 4
    if image is None:
        color = _AVATAR_COLORS[sum(login.encode()) % len(_AVATAR_COLORS)]
        image = Image.new("RGBA", (size, size), (*_AVATAR_BG, 255))
        draw = ImageDraw.Draw(image)
        letter = (login[:1] or "?").upper()
        font = _font(max(1, round(15 * size / _AVATAR_SIZE)), bold=True)
        box = draw.textbbox((0, 0), letter, font=font)
        draw.text(
            (
                (size - (box[2] - box[0])) / 2 - box[0],
                (size - (box[3] - box[1])) / 2 - box[1],
            ),
            letter,
            font=font,
            fill=color,
        )
    else:
        image = image.resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius=radius, fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(image, (0, 0), mask)
    return rounded
