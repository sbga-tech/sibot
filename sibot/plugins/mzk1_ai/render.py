"""PNG cards for `/ai quota`, `/ai rank`, and `/ai reset`."""

import io
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import TypeAlias

from PIL import Image, ImageDraw, ImageFont

from .formatting import (
    PERIOD_LABELS,
    PROVIDER_LABELS,
    account_status,
    forecast_lines,
    format_percent,
    format_short_time,
    reset_credit_lines,
)
from .models import (
    PROVIDERS,
    AccountQuota,
    CodexAccountResetCredits,
    PoolForecast,
    Provider,
    RankingEntry,
    RankingResponse,
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

    def __init__(self) -> None:
        self._ops: list[_Draw] = []
        self.y = _PAD

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
        box = (_PAD, top, _WIDTH - _PAD, self.y)
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
        image = Image.new("RGB", (_WIDTH, self.y + _PAD), _BG)
        draw = ImageDraw.Draw(image)
        for operation in self._ops:
            operation(image, draw)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def _header(canvas: _Canvas, title: str, subtitle: str) -> None:
    canvas.text(_PAD, _Text(title, size=17, bold=True))
    canvas.text(_WIDTH - _PAD, _Text(subtitle, size=11, color=_MUTED), right=True)
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


def render_reset_credits(
    accounts: Sequence[CodexAccountResetCredits], generated_at: datetime
) -> bytes:
    canvas = _Canvas()
    _header(canvas, "Codex 重置机会", format_short_time(generated_at))
    if not accounts:
        canvas.text(_PAD, _Text("暂无可用账号", color=_MUTED))
        canvas.y += canvas.line_height(13)
        return canvas.render()

    top = canvas.y
    panel = canvas.open_panel()
    inner = _PAD + _GAP
    right = _WIDTH - _PAD - _GAP
    for index, account in enumerate(accounts):
        canvas.y += _GAP // 2
        if index:
            canvas.rect((inner, canvas.y, right, canvas.y + 1), _LINE, 0)
        canvas.y += _GAP // 2 + 2 * _SCALE
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
    return canvas.render()


# ---------------------------------------------------------------- rank

_AVATAR_SIZE = 32 * _SCALE


def render_ranking(
    ranking: RankingResponse, avatars: Sequence[Image.Image | None]
) -> bytes:
    canvas = _Canvas()
    subtitle = format_short_time(ranking.generated_at)
    if ranking.stale:
        subtitle += " · 可能已过期"
    _header(canvas, f"Token 排名 · {PERIOD_LABELS[ranking.period]}", subtitle)
    if not ranking.entries:
        canvas.text(_PAD, _Text("暂无数据", color=_MUTED))
        canvas.y += canvas.line_height(13)
        return canvas.render()

    top = canvas.y
    panel = canvas.open_panel()
    canvas.y += _GAP
    for entry, avatar in zip(ranking.entries, avatars, strict=True):
        _ranking_row(canvas, entry, avatar)
    canvas.close_panel(panel, top)
    return canvas.render()


_TOP_COLORS = {1: (202, 138, 4), 2: (100, 116, 139), 3: (180, 83, 9)}


def _ranking_row(
    canvas: _Canvas, entry: RankingEntry, avatar: Image.Image | None
) -> None:
    rank = entry.rank
    login = entry.user.github_login
    name = entry.user.github_name
    value = entry.value
    inner = _PAD + _GAP
    row_top = canvas.y
    rank_text = _Text(
        str(rank), size=14, bold=True, color=_TOP_COLORS.get(rank, _MUTED)
    )
    canvas.y = row_top + (_AVATAR_SIZE - canvas.line_height(14)) // 2
    canvas.text(inner + 18 * _SCALE, rank_text, right=True)
    avatar_x = inner + 26 * _SCALE
    canvas.paste(_avatar(avatar, login), avatar_x, row_top)

    text_x = avatar_x + _AVATAR_SIZE + _GAP
    value_text = _Text(f"{value:,}", size=13)
    value_width = canvas.measure(value_text)
    canvas.y = row_top + (_AVATAR_SIZE - canvas.line_height(13)) // 2
    canvas.text(_WIDTH - _PAD - _GAP, value_text, right=True)

    name_width = _WIDTH - _PAD - _GAP - value_width - _GAP - text_x
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
    canvas.y = row_top + _AVATAR_SIZE + _GAP


def _avatar(image: Image.Image | None, login: str) -> Image.Image:
    size = _AVATAR_SIZE
    if image is None:
        color = _AVATAR_COLORS[sum(login.encode()) % len(_AVATAR_COLORS)]
        image = Image.new("RGBA", (size, size), (*_AVATAR_BG, 255))
        draw = ImageDraw.Draw(image)
        letter = (login[:1] or "?").upper()
        font = _font(15, bold=True)
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
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size, size), radius=8 * _SCALE, fill=255
    )
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(image, (0, 0), mask)
    return rounded
