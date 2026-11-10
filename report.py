"""Статистика COTD и рендер статичной HTML-страницы (для GitHub Pages).

Модуль не делает сетевых запросов: на вход идут строки таблицы, которые
собирает ``main.py``. Страница самодостаточна — CSS и SVG-график встроены,
внешних CDN нет, поэтому файл можно просто положить в репозиторий.
"""

from __future__ import annotations

import html
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import mean, median
from typing import Any

DASH = "—"
WORLD_COLOR = "#4cc2ff"
TREND_COLOR = "#ffd76e"
# Общие файлы сайта: один CSS и один JS на все страницы вместо инлайна.
SITE_ASSETS_DIR = "assets"
SITE_CSS_NAME = "site.css"
SITE_JS_NAME = "site.js"


# ------------------------------------------------------------------ форматирование


def _num(value: float | None, digits: int = 1) -> str:
    """Число с запятой как десятичным разделителем (русская типографика)."""
    if value is None:
        return DASH
    text = f"{value:.{digits}f}"
    return text.replace(".", ",")


def _place(value: float | None) -> str:
    """Место с пробелом между тысячами: 15000 -> '15 000'."""
    if value is None:
        return DASH
    text = f"{round(value):,}".replace(",", " ")
    return text


def _pct(value: float | None, digits: int = 2) -> str:
    """Доля 0..1 -> проценты."""
    if value is None:
        return DASH
    return f"{_num(value * 100, digits)} %"


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 кубок, 2 кубка, 5 кубков."""
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    tail = count % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _short_date(iso: str) -> str:
    """'2026-11-07' -> '07.11.26'."""
    parts = iso.split("-")
    if len(parts) != 3:
        return iso
    return f"{parts[2]}.{parts[1]}.{parts[0][2:]}"


# --------------------------------------------------------------------- статистика


@dataclass
class MapResult:
    """Лучшая/худшая карта."""

    name: str
    map_date: str
    top_pct: float
    place: int | None
    uid: str = ""

    def url(self) -> str:
        """Ссылка на страницу карты на trackmania.io."""
        return f"https://trackmania.io/#/leaderboard/{self.uid}" if self.uid else ""

    def detail(self) -> str:
        pieces = [_short_date(self.map_date), _pct(self.top_pct)]
        if self.place:
            pieces.append(f"место {_place(self.place)}")
        return " · ".join(pieces)


@dataclass
class Progress:
    """Значение метрики в первой и второй половине периода."""

    first: float | None = None
    second: float | None = None
    delta: float | None = None


@dataclass
class Stats:
    name: str
    year: str
    flag: str = ""
    zone: str = ""
    href: str = ""
    total: int = 0
    qualified: int = 0
    avg_div: float | None = None
    avg_rank: float | None = None
    avg_div_rank: float | None = None
    avg_top_pct: float | None = None
    mode_div: int | None = None
    mode_div_count: int = 0
    median_rank: float | None = None
    median_div_rank: float | None = None
    median_top_pct: float | None = None
    first: int = 0
    second: int = 0
    third: int = 0
    top3: int = 0
    top8: int = 0
    top32: int = 0
    best: MapResult | None = None
    worst: MapResult | None = None
    win_rate: float | None = None
    played: int = 0
    unplayed: int = 0
    progress: dict[str, Progress] = field(default_factory=dict)
    prev_year: str = ""
    series: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    tops: list[tuple[str, float | None]] = field(default_factory=list)
    # Дивизион по дням: столбик = дивизион, в котором игрок ехал в этот день.
    div_series: list[tuple[str, int | None]] = field(default_factory=list)


def _is_positive(value: Any) -> bool:
    """Положительное число (bool и None отбрасываются)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _values(rows: Sequence[Mapping[str, Any]], key: str, *, positive: bool = False) -> list[float]:
    """Все числовые значения колонки (None/0 отбрасываются при positive)."""
    result: list[float] = []
    for row in rows:
        value = row.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if positive and value <= 0:
            continue
        result.append(float(value))
    return result


def compute_stats(
    rows: Sequence[Mapping[str, Any]],
    *,
    name: str,
    year: str = "",
    flag: str = "",
    zone: str = "",
    href: str = "",
    map_uids: Mapping[str, str] | None = None,
) -> Stats:
    """Считает все метрики страницы из набора строк."""
    stats = Stats(name=name or "Игрок", year=year, flag=flag, zone=zone, href=href)
    stats.total = len(rows)

    divs = [int(v) for v in _values(rows, "div", positive=True)]
    ranks = [int(v) for v in _values(rows, "rank", positive=True)]
    div_ranks = [int(v) for v in _values(rows, "div_rank", positive=True)]
    tops = _values(rows, "top", positive=True)

    stats.qualified = len(divs)
    # «Сыграно» — есть результат в дивизионе (divrank). Если игрок зашёл в COTD,
    # но раунды не ехал, trackmania.io отдаёт divrank = 0 / score = 0.
    stats.played = sum(1 for row in rows if _is_positive(row.get("div_rank")))
    stats.unplayed = max(0, stats.total - stats.played)
    stats.avg_div = mean(divs) if divs else None
    stats.avg_rank = mean(ranks) if ranks else None
    stats.avg_div_rank = mean(div_ranks) if div_ranks else None
    stats.avg_top_pct = mean(tops) if tops else None

    stats.median_rank = median(ranks) if ranks else None
    stats.median_div_rank = median(div_ranks) if div_ranks else None
    stats.median_top_pct = median(tops) if tops else None

    if divs:
        counter = Counter(divs)
        # При равенстве берём меньший дивизион — он «лучше».
        stats.mode_div, stats.mode_div_count = min(
            counter.items(), key=lambda item: (-item[1], item[0])
        )

    stats.first = sum(1 for value in div_ranks if value == 1)
    stats.second = sum(1 for value in div_ranks if value == 2)
    stats.third = sum(1 for value in div_ranks if value == 3)
    stats.top3 = sum(1 for value in div_ranks if value <= 3)
    stats.top8 = sum(1 for value in div_ranks if value <= 8)
    stats.top32 = sum(1 for value in div_ranks if value <= 32)
    if stats.qualified:
        stats.win_rate = stats.first / stats.qualified

    candidates = []
    for row in rows:
        top = row.get("top")
        if not isinstance(top, (int, float)) or isinstance(top, bool) or top <= 0:
            continue
        place = row.get("rank")
        map_date = str(row.get("map_date") or "")
        candidates.append(
            MapResult(
                name=str(row.get("map_name") or "Без названия"),
                map_date=map_date,
                top_pct=float(top),
                place=int(place) if isinstance(place, (int, float)) and place else None,
                uid=str((map_uids or {}).get(map_date) or ""),
            )
        )

    if candidates:
        stats.best = min(candidates, key=lambda item: item.top_pct)
        stats.worst = max(candidates, key=lambda item: item.top_pct)

    series: list[tuple[str, int | None, int | None]] = []
    top_series: list[tuple[str, float | None]] = []
    div_series: list[tuple[str, int | None]] = []
    for row in rows:
        map_date = str(row.get("map_date") or "")
        if not map_date:
            continue
        rank = row.get("rank")
        div_rank = row.get("div_rank")
        top = row.get("top")
        div = row.get("div")
        series.append(
            (
                map_date,
                int(rank) if isinstance(rank, (int, float)) and rank else None,
                int(div_rank) if isinstance(div_rank, (int, float)) and div_rank else None,
            )
        )
        top_series.append(
            (
                map_date,
                float(top) if isinstance(top, (int, float)) and top and top > 0 else None,
            )
        )
        div_series.append(
            (
                map_date,
                int(div) if isinstance(div, (int, float)) and div and div > 0 else None,
            )
        )
    stats.series = sorted(series, key=lambda item: item[0])
    stats.tops = sorted(top_series, key=lambda item: item[0])
    # COTD — это один день, поэтому и столбик на графике дивизионов один:
    # если за день записей несколько (переигровка), берём лучший дивизион.
    per_day: dict[str, int | None] = {}
    for day, div in div_series:
        current = per_day.get(day)
        if day not in per_day or (div is not None and (current is None or div < current)):
            per_day[day] = div
    stats.div_series = sorted(per_day.items())
    return stats


@dataclass
class Partner:
    """Игрок, с которым чаще всего пересекались в одном дивизионе."""

    name: str
    shared: int
    share: float | None = None
    avg_gap: float | None = None
    flag: str = ""
    zone: str = ""
    href: str = ""


@dataclass
class PlayerRef:
    """Игрок для сравнения: ник, строки и (необязательно) флаг зоны."""

    name: str
    rows: Sequence[Mapping[str, Any]]
    flag: str = ""
    zone: str = ""
    href: str = ""


def _player_cell(name: str, flag: str, zone: str, href: str = "") -> str:
    """Ник с флагом; если знаем ссылку — оборачиваем в <a> на страницу игрока."""
    parts = _flag_img(flag, zone) + html.escape(name)
    if not href:
        return parts
    return f'<a class="player-link" href="{html.escape(href)}">{parts}</a>'


def _pos_head() -> str:
    """Заголовок колонки с номером строки (не сортируется)."""
    return '<th class="cell-pos" title="место в текущей сортировке">№</th>'


def _pos_cell(number: int) -> str:
    """Ячейка с номером строки: пересчитывается при сортировке и фильтре."""
    return f'<td class="cell-pos">{number}</td>'


def _td(key: str, text: str, value: str) -> str:
    """Ячейка таблицы с ключом сортировки; у ника — отдельный класс для выравнивания."""
    css = ' class="cell-name"' if key == "name" else ""
    return f'<td{css} data-key="{key}" data-value="{value}">{text}</td>'


def _flag_img(flag: str, zone: str, *, css: str = "flag") -> str:
    """Картинка флага зоны (data URI из кеша); подсказка — название зоны."""
    if not flag:
        return ""
    title = zone or "зона"
    return (
        f'<img class="{css}" src="{html.escape(flag, quote=True)}" '
        f'alt="{html.escape(title)}" title="{html.escape(title)}" loading="lazy">'
    )


def _div_days(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, int], int | None]:
    """Индексирует строки по паре «день + дивизион» (место в мире как значение).

    Дивизионы COTD стартуют в разное время, поэтому один день и один дивизион
    означают, что игроки ехали один и тот же заезд.
    """
    result: dict[tuple[str, int], int | None] = {}
    for row in rows:
        map_date = str(row.get("map_date") or "")
        div = row.get("div")
        if not map_date or not _is_positive(div):
            continue
        rank = row.get("rank")
        result[(map_date, int(div))] = int(rank) if _is_positive(rank) else None
    return result


def compute_partners(
    rows: Sequence[Mapping[str, Any]],
    others: Sequence[PlayerRef | tuple[str, Sequence[Mapping[str, Any]]]],
) -> list[Partner]:
    """Считает, с кем игрок чаще всего играл: совпадение дня и дивизиона.

    ``others`` — остальные игроки (``PlayerRef`` или пара «имя, строки»).
    Сортировка: больше совместных заездов, затем меньше разница мест.
    """
    mine = _div_days(rows)
    if not mine:
        return []

    partners: list[Partner] = []
    for item in others:
        ref = item if isinstance(item, PlayerRef) else PlayerRef(item[0], item[1])
        theirs = _div_days(ref.rows)
        shared = mine.keys() & theirs.keys()
        if not shared:
            continue
        gaps = [
            abs(mine[key] - theirs[key])
            for key in shared
            if mine[key] is not None and theirs[key] is not None
        ]
        partners.append(
            Partner(
                name=ref.name,
                shared=len(shared),
                share=len(shared) / len(mine),
                avg_gap=mean(gaps) if gaps else None,
                flag=ref.flag,
                zone=ref.zone,
                href=ref.href,
            )
        )

    partners.sort(key=lambda item: (-item.shared, item.avg_gap or 0.0, item.name.casefold()))
    return partners


# Поля Stats, для которых считаем разницу с предыдущим годом.
PROGRESS_METRICS: tuple[str, ...] = (
    "avg_div",
    "avg_rank",
    "avg_div_rank",
    "avg_top_pct",
    "median_rank",
    "median_div_rank",
    "median_top_pct",
    "mode_div",
)

# Сколько стартов нужно в году, чтобы сравнивать его с соседним.
PROGRESS_MIN_STARTS = 5


def compute_year_progress(current: Stats, previous: Stats) -> dict[str, Progress]:
    """Сравнивает метрики года с предыдущим годом (прогресс год к году)."""
    progress: dict[str, Progress] = {}
    for name in PROGRESS_METRICS:
        now = getattr(current, name, None)
        before = getattr(previous, name, None)
        if not isinstance(now, (int, float)) or not isinstance(before, (int, float)):
            continue
        progress[name] = Progress(
            first=float(before), second=float(now), delta=float(now) - float(before)
        )
    return progress


# ----------------------------------------------------------------------- график


def _percentile(values: Sequence[float], fraction: float = 0.95) -> float:
    """Значение на заданном перцентиле (без интерполяции: просто срез).

    Используется, чтобы редкие провальные дни не растягивали ось графиков.
    ``fraction=0.95`` — 95-й перцентиль: 5 % худших значений оказываются за
    границей шкалы.
    """
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _series_points(
    series: Sequence[tuple[str, int | None, int | None]],
    index: int,
    x_of: Any,
    y_of: Any,
    label_of: Any,
) -> list[tuple[float, float, str]]:
    """Точки ряда: (x, y, подпись). Дни без результата пропускаются."""
    points: list[tuple[float, float, str]] = []
    for position, item in enumerate(series):
        value = item[index]
        if not value:
            continue
        points.append((x_of(position), y_of(value), label_of(item[0], value)))
    return points


def _dot(x: float, y: float, color: str, label: str, *, visible: bool = True) -> str:
    """Точка графика: зона наведения с подсказкой (data-label + <title> как fallback)."""
    hit = (
        f'<circle class="dot-hit" cx="{x:.1f}" cy="{y:.1f}" r="10" '
        f'data-label="{html.escape(label)}"><title>{html.escape(label)}</title></circle>'
    )
    if not visible:
        return hit
    return hit + f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{color}"/>'


def _trend_endpoints(
    points: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Линейный тренд по точкам графика (в экранных координатах).

    Ось Y логарифмическая, поэтому прямая в этих координатах — это
    экспоненциальный тренд по местам: наклон вверх = результаты улучшаются.
    """
    if len(points) < 2:
        return None
    count = len(points)
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if not denominator:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    intercept = mean_y - slope * mean_x
    first_x = points[0][0]
    last_x = points[-1][0]
    return (first_x, slope * first_x + intercept), (last_x, slope * last_x + intercept)


def _x_axis_labels(dates: Sequence[str], x_of: Any, y: float) -> list[str]:
    """Подписи оси X: первая, последняя и несколько между ними."""
    ticks = min(6, len(dates))
    if ticks > 1:
        indexes = sorted({round(i * (len(dates) - 1) / (ticks - 1)) for i in range(ticks)})
    else:
        indexes = [0]

    parts: list[str] = []
    for index in indexes:
        x = x_of(index)
        if index == 0:
            anchor = "start"
        elif index == len(dates) - 1:
            anchor = "end"
        else:
            anchor = "middle"
        parts.append(
            f'<text class="axis" x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}">'
            f"{html.escape(_short_date(dates[index]))}</text>"
        )
    return parts


def _axis_percent(value: float) -> str:
    """Подпись оси в процентах: целые без дробной части, иначе один знак."""
    if value == 0:
        return "0 %"
    if abs(value - round(value)) < 1e-9:
        return f"{value:.0f} %"
    return f"{_num(value, 1)} %"


def _chart_svg(stats: Stats, *, width: int = 880, height: int = 360) -> str:
    """SVG-график мест в мире по дням: ось Y логарифмическая, ниже — лучше.

    Верх шкалы — 95-й перцентиль: редкие провальные дни иначе растягивают ось на
    порядок и весь остальной график сжимается. Такие точки прижимаются к верхней
    границе (выше — хуже), точное место видно в подсказке.
    """
    series = stats.series
    values = [world for _, world, _ in series if world]
    if len(series) < 2 or not values:
        return ""

    left, right, top, bottom = 64, 18, 16, 40
    plot_w = width - left - right
    plot_h = height - top - bottom

    lo_exp = math.floor(math.log10(min(values)))
    hi_exp = math.ceil(math.log10(_percentile(values)))
    if hi_exp <= lo_exp:
        hi_exp = lo_exp + 1

    def y_of(value: float) -> float:
        fraction = (math.log10(value) - lo_exp) / (hi_exp - lo_exp)
        return top + plot_h * (1 - min(1.0, max(0.0, fraction)))

    def x_of(index: int) -> float:
        return left + plot_w * index / (len(series) - 1)

    parts: list[str] = [
        (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Места в COTD по дням" xmlns="http://www.w3.org/2000/svg">'
        )
    ]

    # Горизонтальная сетка с подписями (1, 10, 100, 1 000, 10 000).
    for exp in range(lo_exp, hi_exp + 1):
        value = 10**exp
        y = y_of(value)
        parts.append(
            f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis axis--y" x="{left - 12}" y="{y + 4:.1f}">{_place(value)}</text>'
        )

    parts.append(
        f'<line class="base" x1="{left}" y1="{top + plot_h}" '
        f'x2="{left + plot_w}" y2="{top + plot_h}"/>'
    )

    # Подписи по X: первая, последняя и несколько между ними.
    parts.extend(_x_axis_labels([item[0] for item in series], x_of, top + plot_h + 26))

    # Место в мире: точки дней и соединяющая линия.
    points = _series_points(
        series, 1, x_of, y_of, lambda day, value: f"{_short_date(day)} · место {_place(value)}"
    )
    if len(points) > 1:
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
        parts.append(f'<polyline class="line" points="{path}" fill="none" stroke="{WORLD_COLOR}"/>')
    visible_dots = len(series) <= 150
    for x, y, label in points:
        parts.append(_dot(x, y, WORLD_COLOR, label, visible=visible_dots))

    # Тренд: прямая по тем же точкам.
    trend = _trend_endpoints([(x, y) for x, y, _ in points])
    if trend is not None:
        (x1, y1), (x2, y2) = trend
        parts.append(
            f'<line class="trend" x1="{x1:.1f}" y1="{y1:.1f}" '
            f'x2="{x2:.1f}" y2="{y2:.1f}" stroke="{TREND_COLOR}"/>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _chart_top_svg(stats: Stats, *, width: int = 880, height: int = 320) -> str:
    """SVG-график топа по миру: шкала от 0 до 95-го перцентиля, ниже — лучше.

    Без обрезки один провальный день (например, 65 % при медиане 12 %) растягивает
    ось до 100 % и весь график сжимается к низу. Значения за границей шкалы
    прижимаются к верхней линии (выше — хуже), точное значение видно в подсказке.
    """
    tops = stats.tops
    values = [pct * 100 for _, pct in tops if pct is not None]
    if len(tops) < 2 or len(values) < 2:
        return ""

    left, right, top, bottom = 64, 18, 16, 40
    plot_w = width - left - right
    plot_h = height - top - bottom

    # Топ по миру — доля игроков впереди; предел шкалы обрезаем по p95.
    y_max = max(1.0, _percentile(values))

    def y_of(percent: float) -> float:
        return top + plot_h * (1 - min(1.0, max(0.0, percent / y_max)))

    def x_of(index: int) -> float:
        return left + plot_w * index / (len(tops) - 1)

    parts: list[str] = [
        (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Топ по миру в процентах по дням" '
            f'xmlns="http://www.w3.org/2000/svg">'
        )
    ]

    steps = 4
    for step in range(steps + 1):
        value = y_max * step / steps
        y = y_of(value)
        parts.append(
            f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis axis--y" x="{left - 12}" y="{y + 4:.1f}">'
            f"{html.escape(_axis_percent(value))}</text>"
        )

    parts.append(
        f'<line class="base" x1="{left}" y1="{top + plot_h}" '
        f'x2="{left + plot_w}" y2="{top + plot_h}"/>'
    )

    parts.extend(_x_axis_labels([item[0] for item in tops], x_of, top + plot_h + 26))

    # Топ по миру: точки дней и соединяющая линия.
    points = [
        (x_of(index), y_of(pct * 100), f"{_short_date(day)} · топ {_pct(pct)}")
        for index, (day, pct) in enumerate(tops)
        if pct is not None
    ]
    if len(points) > 1:
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
        parts.append(f'<polyline class="line" points="{path}" fill="none" stroke="{WORLD_COLOR}"/>')
    visible_dots = len(tops) <= 150
    for x, y, label in points:
        parts.append(_dot(x, y, WORLD_COLOR, label, visible=visible_dots))

    # Тренд по тем же точкам.
    trend = _trend_endpoints([(x, y) for x, y, _ in points])
    if trend is not None:
        (x1, y1), (x2, y2) = trend
        parts.append(
            f'<line class="trend" x1="{x1:.1f}" y1="{y1:.1f}" '
            f'x2="{x2:.1f}" y2="{y2:.1f}" stroke="{TREND_COLOR}"/>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _chart_div_svg(stats: Stats, *, width: int = 880, height: int = 300) -> str:
    """SVG-гистограмма дивизионов: один столбик — один день COTD.

    Чем ниже столбик, тем ближе дивизион к 1-му (ниже — сильнее): высота — это
    удалённость от первого дивизиона, как «ниже — лучше» на графиках места.
    Шкала обрезана по 95-му перцентилю: иначе один слабый день растягивает ось
    и вся основная масса столбиков сливается у нижней границы. Такие дни
    прижимаются к верху, точный дивизион виден в подсказке. Дни без дивизиона
    (не квалифицировался) столбика не получают, но место на оси X занимают —
    чтобы шкала времени не сжималась.
    """
    items = stats.div_series
    values = sorted(div for _, div in items if div)
    if len(items) < 2 or not values:
        return ""

    left, right, top, bottom = 64, 18, 16, 40
    plot_w = width - left - right
    plot_h = height - top - bottom

    best = values[0]
    worst = max(_percentile(values), best + 1)
    span = worst - best

    def y_of(div: float) -> float:
        """Экранная Y верхушки столбика: 1-й дивизион — у самого низа."""
        fraction = (div - best + 1) / (span + 1)
        return top + plot_h * (1 - min(1.0, max(0.0, fraction)))

    slot = plot_w / len(items)
    bar_w = max(1.2, min(18.0, slot * 0.72))

    def x_of(index: int) -> float:
        """Центр столбика по его номеру (для подписей оси X)."""
        return left + slot * index + slot / 2

    parts: list[str] = [
        (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Дивизионы по дням" xmlns="http://www.w3.org/2000/svg">'
        )
    ]

    # Сетка по дивизионам: 1-й у нижней границы, граница шкалы сверху.
    ticks = {best, worst, *(round(best + span * step / 3) for step in range(1, 3))}
    for div in sorted(ticks):
        y = y_of(div)
        parts.append(
            f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}"/>'
        )
        parts.append(f'<text class="axis axis--y" x="{left - 12}" y="{y + 4:.1f}">{div}</text>')

    parts.append(
        f'<line class="base" x1="{left}" y1="{top + plot_h}" '
        f'x2="{left + plot_w}" y2="{top + plot_h}"/>'
    )

    parts.extend(_x_axis_labels([item[0] for item in items], x_of, top + plot_h + 26))

    bars: list[str] = []
    hits: list[str] = []
    for index, (day, div) in enumerate(items):
        centre = x_of(index)
        if not div:
            continue
        y = y_of(div)
        bar_h = max(1.5, top + plot_h - y)
        bars.append(
            f'<rect class="bar" x="{centre - bar_w / 2:.1f}" y="{y:.1f}" '
            f'width="{bar_w:.1f}" height="{bar_h:.1f}" rx="1.5" fill="{WORLD_COLOR}"/>'
        )
        label = f"{_short_date(day)} · дивизион {div}"
        hits.append(
            f'<rect class="dot-hit bar-hit" x="{centre - slot / 2:.1f}" y="{top}" '
            f'width="{max(1.0, slot):.1f}" height="{plot_h}" '
            f'data-label="{html.escape(label)}"><title>{html.escape(label)}</title></rect>'
        )
    parts.extend(bars)
    parts.extend(hits)
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------------------- HTML

CSS = """
:root {
  --bg: #0b0e14;
  --bg-soft: #101725;
  --card: #151c2b;
  --card-edge: #1f2a3d;
  --text: #e8eef8;
  --muted: #8b9bb4;
  --accent: #4cc2ff;
  --accent-2: #7ee787;
  --gold: #ffd76e;
  --silver: #cfd8e6;
  --bronze: #e0a273;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--text);
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  background:
    radial-gradient(1100px 560px at 18% -12%, #17263d 0%, rgba(11, 14, 20, 0) 62%),
    var(--bg);
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 44px 20px 60px; }
.hero {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.hero__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
/* Переключатель периода живёт в одной строке с навигацией — прижимаем вправо. */
.hero__bar .year-pick { margin-left: auto; }
.hero--player .hero__bar {
  padding-bottom: 12px;
  border-bottom: 1px solid var(--card-edge);
}
.hero__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.hero__top .eyebrow { margin: 0; }
.hero__body {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  flex-wrap: wrap;
}
.hero__main {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
}
.hero__side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 14px;
}
.eyebrow {
  margin: 0 0 8px;
  color: var(--accent);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
h1 { margin: 0; font-size: clamp(30px, 5vw, 52px); letter-spacing: -0.02em; line-height: 1.1; }
.flag {
  width: 20px;
  height: 14px;
  margin-right: 6px;
  object-fit: cover;
  vertical-align: -2px;
  border-radius: 2px;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.14);
}
.flag--hero {
  width: 34px;
  height: 24px;
  margin-right: 12px;
  vertical-align: 3px;
  border-radius: 3px;
}
.sub { margin: 10px 0 0; color: var(--muted); }
.badge {
  min-width: 128px;
  padding: 14px 22px;
  text-align: center;
  background: var(--card);
  border: 1px solid var(--card-edge);
  border-radius: 18px;
}
.badges { display: flex; gap: 12px; flex-wrap: wrap; }
.badge--played { border-color: rgba(110, 231, 168, 0.35); }
.badge--played b { color: #6ee7a8; }
.badge b { display: block; font-size: 30px; line-height: 1.1; }
.badge small { color: var(--muted); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; }
section { margin-top: 42px; }
h2 {
  margin: 0 0 14px;
  color: var(--muted);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.grid { display: grid; gap: 12px; }
/* Раскладки фиксированные: 4–5 карточек — в один ряд. */
.grid--5 { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.grid--4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.grid--5 .card { padding: 14px 16px; }
.grid--5 .card__value { font-size: 24px; }
.grid--5 .card__value--text { font-size: 16px; }
@media (max-width: 980px) {
  .grid--5, .grid--4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  /* нечётный «хвост» растягиваем на всю строку, чтобы не было дырки справа */
  .grid--5 > .card:last-child:nth-child(odd) { grid-column: 1 / -1; }
}
@media (max-width: 560px) {
  .grid--5, .grid--4 { grid-template-columns: minmax(0, 1fr); }
  .grid--5 > .card:last-child:nth-child(odd) { grid-column: auto; }
}
.card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 16px 18px;
  background: linear-gradient(180deg, var(--card) 0%, var(--bg-soft) 100%);
  border: 1px solid var(--card-edge);
  border-radius: 14px;
}
.card__label { color: var(--muted); font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; hyphens: auto; }
.card__value { font-size: 27px; font-weight: 700; letter-spacing: -0.02em; }
.card__value--text { font-size: 18px; line-height: 1.35; }
.card__hint { margin: 0; color: var(--muted); font-size: 12.5px; }
.card--gold { border-color: #66562a; }
.card--gold .card__value { color: var(--gold); }
.card--silver { border-color: #4b5566; }
.card--silver .card__value { color: var(--silver); }
.card--bronze { border-color: #6b4a30; }
.card--bronze .card__value { color: var(--bronze); }
.card--best { border-color: #2f6f45; }
.card--best .card__value { color: var(--accent-2); }
.card--worst { border-color: #743b3b; }
.card--worst .card__value { color: #ff8b81; }
.delta {
  margin-left: 8px;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0;
  vertical-align: 3px;
  white-space: nowrap;
}
.delta--good { color: #6ee7a8; background: rgba(110, 231, 168, 0.12); }
.delta--bad { color: #ff8a8a; background: rgba(255, 138, 138, 0.12); }
.delta--flat { color: var(--muted); background: rgba(255, 255, 255, 0.06); }
.delta--inline {
  margin-left: 0;
  padding: 0 4px;
  font-size: inherit;
  line-height: inherit;
  vertical-align: baseline;
  border-radius: 5px;
}
.maps { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.legend { display: flex; gap: 18px; margin: 0 2px 10px; color: var(--muted); font-size: 13px; }
.legend i { display: inline-block; width: 10px; height: 10px; margin-right: 7px; border-radius: 3px; }
.legend .legend__dash {
  width: 16px;
  height: 3px;
  border-radius: 2px;
  background: repeating-linear-gradient(90deg, #ffd76e 0 4px, rgba(255, 215, 110, 0.18) 4px 8px);
  vertical-align: middle;
}
.chart {
  padding: 12px 8px 4px;
  background: var(--card);
  border: 1px solid var(--card-edge);
  border-radius: 14px;
  overflow-x: auto;
}
.chart svg { display: block; width: 100%; height: auto; min-width: 560px; }
.grid-line, .chart .grid { stroke: #1c2637; stroke-width: 1; }
.chart .base { stroke: #2b3a52; stroke-width: 1; }
.chart .line { stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; opacity: 0.92; }
.chart .trend { stroke-width: 2; stroke-dasharray: 7 6; opacity: 0.9; }
.chart .axis { fill: var(--muted); font-size: 11px; font-family: inherit; }
.chart .axis--y { text-anchor: end; }
.chart .dot { stroke: var(--bg); stroke-width: 1; }
.chart .dot-hit { fill: transparent; stroke: none; cursor: pointer; }
.chart .dot-hit:focus { outline: none; }
.chart .bar { fill-opacity: 0.8; }
.chart .bar-hit:hover { fill: rgba(76, 194, 255, 0.12); }
.chart-tip[hidden] { display: none; }
.chart-tip {
  position: absolute;
  z-index: 20;
  padding: 6px 10px;
  border-radius: 8px;
  background: rgba(9, 12, 18, 0.96);
  border: 1px solid var(--card-edge);
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.45);
  color: var(--text);
  font-size: 12.5px;
  line-height: 1.3;
  white-space: nowrap;
  pointer-events: none;
}
.note { margin: 10px 2px 0; color: var(--muted); font-size: 12.5px; }
.empty {
  padding: 18px 20px;
  background: #2a1f1f;
  border: 1px solid #6b3b3b;
  border-radius: 14px;
  color: #ffd0cb;
}
.footer { margin-top: 46px; color: var(--muted); font-size: 12px; }
.nav__home {
  display: inline-flex;
  align-items: center;
  align-self: flex-start;
  padding: 6px 14px;
  border: 1px solid var(--accent);
  border-radius: 999px;
  color: var(--accent);
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}
.nav__home:hover { background: rgba(76, 194, 255, 0.12); }
.nav__home.is-active {
  border-color: var(--card-edge);
  color: var(--muted);
  pointer-events: none;
}
.year-pick { position: relative; }
.year-pick summary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border: 1px solid var(--card-edge);
  border-radius: 999px;
  color: var(--muted);
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  list-style: none;
  cursor: pointer;
  user-select: none;
}
.year-pick summary::-webkit-details-marker { display: none; }
.year-pick summary:hover { color: var(--text); border-color: var(--accent); }
.year-pick[open] summary { color: var(--text); border-color: var(--accent); }
.year-pick summary b { letter-spacing: 0; font-size: 13px; color: var(--text); }
.year-pick summary::after {
  content: "▾";
  font-size: 11px;
  color: var(--muted);
}
.year-pick[open] summary::after { content: "▴"; }
.year-pick__list {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  z-index: 30;
  min-width: 108px;
  margin: 0;
  padding: 6px;
  list-style: none;
  background: #131a26;
  border: 1px solid var(--card-edge);
  border-radius: 12px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
}
.year-pick__list a {
  display: block;
  padding: 7px 12px;
  border-radius: 8px;
  color: var(--text);
  font-size: 13px;
  text-decoration: none;
}
.year-pick__list a:hover { background: rgba(76, 194, 255, 0.14); color: var(--accent); }
.year-pick__list a.is-current { color: var(--accent); font-weight: 700; }
.table-wrap {
  overflow-x: auto;
  background: var(--card);
  border: 1px solid var(--card-edge);
  border-radius: 14px;
}
.table { width: 100%; border-collapse: collapse; font-size: 14px; min-width: 720px; }
.table th.sortable { cursor: pointer; user-select: none; }
.table th.sortable:hover { color: var(--text); }
.table th.is-sorted { color: var(--accent); }
.table th.is-sorted::after {
  position: absolute;
  top: 50%;
  right: 6px;
  transform: translateY(-50%);
  content: "↑";
}
.table th.is-sorted[data-order="desc"]::after { content: "↓"; }
.table th, .table td { padding: 11px 14px; text-align: right; white-space: nowrap; }
.table th { position: relative; padding-right: 22px; white-space: normal; }
.table .cell-name { text-align: left; }
/* Колонка «№»: номер строки в текущей сортировке (пересчитывается скриптом). */
.table .cell-pos {
  width: 1%;
  padding-right: 4px;
  padding-left: 14px;
  color: var(--muted);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.table thead th {
  color: var(--muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  border-bottom: 1px solid var(--card-edge);
}
.table tbody tr + tr td { border-top: 1px solid rgba(31, 42, 61, 0.6); }
.table tbody tr.is-current { background: rgba(76, 194, 255, 0.08); }
.player-link {
  color: inherit;
  text-decoration: none;
  border-bottom: 1px solid transparent;
}
.player-link:hover { color: var(--accent); border-bottom-color: var(--accent); }
.map-link {
  color: inherit;
  text-decoration: none;
  border-bottom: 1px solid transparent;
}
.map-link:hover { color: var(--accent); border-bottom-color: var(--accent); }
.tabs {
  display: inline-flex;
  gap: 6px;
  margin-bottom: 16px;
  padding: 4px;
  background: var(--card);
  border: 1px solid var(--card-edge);
  border-radius: 999px;
}
.tab {
  padding: 7px 16px;
  background: transparent;
  border: 0;
  border-radius: 999px;
  color: var(--muted);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.tab:hover { color: var(--text); }
.tab.is-active { background: rgba(76, 194, 255, 0.15); color: var(--accent); }
.tab:focus-visible { outline: 1px solid var(--accent); outline-offset: 2px; }
.table-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.min-cotd { position: relative; }
.min-cotd summary {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  border: 1px solid var(--card-edge);
  border-radius: 999px;
  color: var(--muted);
  font-size: 12px;
  list-style: none;
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}
.min-cotd summary::-webkit-details-marker { display: none; }
.min-cotd summary:hover { color: var(--text); border-color: var(--accent); }
.min-cotd[open] summary { color: var(--text); border-color: var(--accent); }
.min-cotd summary b { font-weight: 600; letter-spacing: 0.06em; }
.min-cotd summary::after { content: "▾"; font-size: 10px; color: var(--muted); }
.min-cotd[open] summary::after { content: "▴"; }
.min-cotd__list {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  z-index: 30;
  min-width: 96px;
  margin: 0;
  padding: 6px;
  list-style: none;
  background: #131a26;
  border: 1px solid var(--card-edge);
  border-radius: 12px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
}
.min-cotd__option {
  display: block;
  width: 100%;
  padding: 6px 12px;
  background: transparent;
  border: 0;
  border-radius: 8px;
  color: var(--text);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.min-cotd__option:hover { background: rgba(76, 194, 255, 0.14); color: var(--accent); }
.min-cotd__option.is-current { color: var(--accent); font-weight: 700; }
.table tbody tr.is-current .cell-name { color: var(--accent); font-weight: 600; }
.footer a { color: var(--accent); text-decoration: none; }
@media (max-width: 620px) {
  .wrap { padding-top: 28px; }
  .hero__side { align-items: flex-start; }
  .badges { gap: 8px; }
  .badge { min-width: auto; padding: 10px 14px; }
  .badge b { font-size: 22px; }
  .card__value { font-size: 23px; }
}
""".strip()


def _num_short(value: float | None) -> str:
    """Число без лишнего «,0»: 1,0 -> 1; 0,4 -> 0,4."""
    text = _num(value, 1)
    if text.endswith(",0"):
        return text[:-2]
    return text


def _delta_html(
    progress: Progress | None,
    *,
    lower_is_better: bool = True,
    prev_year: str = "",
    percent: bool = False,
    digits: int = 1,
) -> str:
    """Дельта «стало − было»: зелёная, если лучше, красная — если хуже.

    ``percent=True`` — значения заданы долями 0..1, поэтому дельта показывается
    в процентных пунктах (0,0029 -> −0,29).
    """
    if progress is None or progress.delta is None:
        return ""
    delta = progress.delta
    scale = 100.0 if percent else 1.0
    title = (
        f"{prev_year}: {_pct(progress.first)} → сейчас: {_pct(progress.second)}"
        if percent and prev_year
        else f"{prev_year}: {_num_short(progress.first)} → сейчас: {_num_short(progress.second)}"
        if prev_year
        else f"было {_num_short(progress.first)} → стало {_num_short(progress.second)}"
    )
    if abs(delta) * scale < 0.05:
        return f'<span class="delta delta--flat" title="{html.escape(title)}">±0</span>'
    better = delta < 0 if lower_is_better else delta > 0
    css = "delta delta--good" if better else "delta delta--bad"
    sign = "−" if delta < 0 else "+"
    value = _num_short(abs(delta) * scale) if digits == 1 else _num(abs(delta) * scale, digits)
    return f'<span class="{css}" title="{html.escape(title)}">{sign}{value}</span>'


def _card(
    label: str,
    value: str,
    hint: str = "",
    modifier: str = "",
    delta_html: str = "",
) -> str:
    """Одна карточка с метрикой (и необязательной дельтой прогресса)."""
    classes = "card" + (f" card--{modifier}" if modifier else "")
    value_class = "card__value"
    if len(value) > 12:
        value_class += " card__value--text"
    hint_html = f'<p class="card__hint">{html.escape(hint)}</p>' if hint else ""
    return (
        f'<article class="{classes}">'
        f'<span class="card__label">{html.escape(label)}</span>'
        f'<span class="{value_class}">{html.escape(value)}{delta_html}</span>'
        f"{hint_html}"
        "</article>"
    )


def _map_card(label: str, result: MapResult | None, modifier: str) -> str:
    """Карточка лучшей/худшей карты."""
    if result is None:
        return _card(label, DASH, "нет данных", modifier)
    name_html = html.escape(result.name)
    if result.uid:
        name_html = (
            f'<a class="map-link" href="{html.escape(result.url())}" target="_blank" '
            f'rel="noopener noreferrer" title="Открыть карту на trackmania.io">{name_html}</a>'
        )
    return (
        f'<article class="card card--{modifier}">'
        f'<span class="card__label">{html.escape(label)}</span>'
        f'<span class="card__value card__value--text">{name_html}</span>'
        f'<p class="card__hint">{html.escape(result.detail())}</p>'
        "</article>"
    )


ALL_TIME_KEY = "all"
ALL_TIME_LABEL = "всё время"


def _period_label(period: int | str) -> str:
    """Подпись периода: «всё время» или год."""
    return ALL_TIME_LABEL if str(period) == ALL_TIME_KEY else str(period)


def render_year_select(years: Sequence[int | str], current: int | str, *, page: str) -> str:
    """Выпадающий список периодов на `details` (нативный select игнорирует тёмные цвета).

    В списке могут быть годы и ``all`` («всё время»). Ссылки ведут на ту же
    страницу в каталоге другого периода.
    """
    if not years:
        return ""
    this = str(current)
    items: list[str] = []
    for year in years:
        value = str(year)
        href = page if value == this else f"../{value}/{page}"
        label = html.escape(_period_label(value))
        if value == this:
            items.append(
                f'<li><a class="is-current" href="{html.escape(href)}" '
                f'aria-current="true">{label}</a></li>'
            )
        else:
            items.append(f'<li><a href="{html.escape(href)}">{label}</a></li>')
    summary = html.escape(_period_label(this))
    prefix = "" if this == ALL_TIME_KEY else "год "
    return (
        '<details class="year-pick">'
        f"<summary>{prefix}<b>{summary}</b></summary>"
        f'<ul class="year-pick__list">{"".join(items)}</ul>'
        "</details>"
    )


def render_redirect(target: str, years: Sequence[int | str]) -> str:
    """Корневая страница сайта: перекидывает на последний год и перечисляет остальные."""
    links = "".join(
        f'<li><a href="{html.escape(str(year))}/index.html">{html.escape(str(year))}</a></li>'
        for year in years
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta http-equiv="refresh" content="0; url={html.escape(target)}">
<title>COTD · выбор года</title>
<style>
body {{ margin: 0; padding: 60px 20px; background: #0b0e14; color: #e7ecf5;
       font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }}
main {{ max-width: 640px; margin: 0 auto; }}
h1 {{ font-size: 28px; margin: 0 0 12px; }}
p {{ color: #8b97ad; }}
a {{ color: #4cc2ff; }}
ul {{ display: flex; gap: 10px; flex-wrap: wrap; list-style: none; padding: 0; margin: 20px 0 0; }}
li a {{ display: inline-block; padding: 8px 16px; border: 1px solid #223047;
        border-radius: 999px; text-decoration: none; }}
</style>
</head>
<body>
<main>
  <h1>COTD: статистика по игрокам</h1>
  <p>Открываю <a href="{html.escape(target)}">{html.escape(target)}</a>. Если переход не сработал,
  выбери год:</p>
  <ul>{links}</ul>
</main>
</body>
</html>
"""


def render_home_link(home: tuple[str, str, bool] | None) -> str:
    """Ссылка-«крошка» на главную таблицу года (на самой главной её нет)."""
    if home is None:
        return ""
    label, href, is_active = home
    class_attr = "nav__home" + (" is-active" if is_active else "")
    current = ' aria-current="page"' if is_active else ""
    arrow = "" if is_active else "← "
    return (
        f'<a class="{class_attr}" href="{html.escape(href)}"{current}>'
        f"{arrow}{html.escape(label)}</a>"
    )


def _script_body(script: str) -> str:
    """Код скрипта без тега ``<script>`` — для общего файла сайта."""
    return script.strip().removeprefix("<script>").removesuffix("</script>").strip()


def site_css() -> str:
    """Общий CSS сайта (один файл на все страницы вместо инлайна)."""
    return CSS


def site_js() -> str:
    """Все клиентские скрипты одним файлом.

    Каждый блок сам проверяет, есть ли на странице его элементы, поэтому файл
    безопасно подключать везде.
    """
    return "\n\n".join(
        _script_body(script)
        for script in (
            TABS_SCRIPT,
            MIN_COTD_SCRIPT,
            YEAR_PICK_SCRIPT,
            SORT_SCRIPT,
            CHART_TIP_SCRIPT,
        )
    )


def _document(*, title: str, content: str, footer: str, assets: str = "") -> str:
    """Общий каркас страницы: head, стили, контент, футер, скрипты.

    ``assets`` — префикс к общим файлам сайта (например, ``../assets/``). Если он
    задан, CSS и JS подключаются отдельными файлами и кешируются браузером на
    всех страницах; пустая строка — самодостаточная страница (одиночный
    ``--html``) со встроенными стилями и скриптами.
    """
    if assets:
        head = f'<link rel="stylesheet" href="{html.escape(assets)}{SITE_CSS_NAME}">'
        tail = f'\n<script src="{html.escape(assets)}{SITE_JS_NAME}" defer></script>\n'
    else:
        scripts: list[str] = []
        if 'class="tab' in content:
            scripts.append(TABS_SCRIPT)
        if "data-min-cotd" in content:
            scripts.append(MIN_COTD_SCRIPT)
        if 'class="year-pick"' in content:
            scripts.append(YEAR_PICK_SCRIPT)
        if 'class="table"' in content:
            scripts.append(SORT_SCRIPT)
        if "dot-hit" in content:
            scripts.append(CHART_TIP_SCRIPT)
        head = f"<style>\n{CSS}\n</style>"
        tail = "".join(f"\n{script}\n" for script in scripts)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="generator" content="cotd-recap">
<title>{html.escape(title)}</title>
{head}
</head>
<body>
<div class="wrap">
{content}
  <p class="footer">{footer}</p>
</div>{tail}
</body>
</html>
"""


COMPARE_SORT_KEYS: dict[str, str] = {
    "cotd": "s.total",
    "avg_div": "s.avg_div",
    "avg_rank": "s.avg_rank",
    "avg_top": "s.avg_top_pct",
    "avg_div_rank": "s.avg_div_rank",
    "mode_div": "s.mode_div",
    "median_rank": "s.median_rank",
    "median_div_rank": "s.median_div_rank",
    "median_top": "s.median_top_pct",
}

SORT_SCRIPT = """
<script>
(function () {
  function valueOf(row, key) {
    var cell = row.querySelector('[data-key="' + key + '"]');
    return cell ? cell.getAttribute("data-value") : null;
  }
  // Номера строк считаются от текущего порядка: после сортировки и фильтра
  // первый в списке — 1, скрытые строки номер не занимают.
  function renumber(table) {
    if (!table.tBodies.length) return;
    var n = 0;
    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      var cell = row.querySelector(".cell-pos");
      if (!cell) return;
      if (row.hidden) { cell.textContent = ""; return; }
      n += 1;
      cell.textContent = String(n);
    });
  }
  document.querySelectorAll("table.table").forEach(function (table) {
    var body = table.tBodies[0];
    table.tHead.querySelectorAll("th").forEach(function (th) {
      var key = th.getAttribute("data-key");
      if (!key) return; // колонка «№» не сортируется
      th.classList.add("sortable");
      th.addEventListener("click", function () {
        var ascending = th.getAttribute("data-order") !== "asc";
        table.tHead.querySelectorAll("th").forEach(function (other) {
          other.removeAttribute("data-order");
          other.classList.remove("is-sorted");
        });
        th.setAttribute("data-order", ascending ? "asc" : "desc");
        th.classList.add("is-sorted");
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var av = valueOf(a, key);
          var bv = valueOf(b, key);
          if (av === "" || av === null) return 1;
          if (bv === "" || bv === null) return -1;
          var an = parseFloat(av);
          var bn = parseFloat(bv);
          var result = (!isNaN(an) && !isNaN(bn))
            ? an - bn
            : String(av).localeCompare(String(bv), "ru");
          return ascending ? result : -result;
        });
        rows.forEach(function (row) { body.appendChild(row); });
        renumber(table);
      });
    });
    renumber(table);
  });
})();
</script>
""".strip()


YEAR_PICK_SCRIPT = """
<script>
(function () {
  var pickers = document.querySelectorAll("details.year-pick");
  if (!pickers.length) return;
  document.addEventListener("click", function (event) {
    pickers.forEach(function (picker) {
      if (picker.open && !picker.contains(event.target)) picker.open = false;
    });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    pickers.forEach(function (picker) { picker.open = false; });
  });
})();
</script>
""".strip()


TABS_SCRIPT = """
<script>
(function () {
  var tabs = document.querySelectorAll(".tab[data-tab]");
  if (!tabs.length) return;
  var panels = document.querySelectorAll(".panel[data-panel]");
  function activate(name) {
    tabs.forEach(function (tab) {
      var active = tab.getAttribute("data-tab") === name;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-panel") !== name;
    });
  }
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      activate(tab.getAttribute("data-tab"));
    });
  });
  var initial = document.querySelector(".tab.is-active");
  if (initial) activate(initial.getAttribute("data-tab"));
})();
</script>
""".strip()


CHART_TIP_SCRIPT = """
<script>
(function () {
  var dots = document.querySelectorAll(".dot-hit");
  if (!dots.length) return;
  var tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  document.body.appendChild(tip);

  function show(dot, event) {
    var label = dot.getAttribute("data-label");
    if (!label) return;
    tip.textContent = label;
    tip.hidden = false;
    place(event);
  }
  function place(event) {
    var pad = 14;
    var x = event.pageX + pad;
    var y = event.pageY - tip.offsetHeight - 10;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  dots.forEach(function (dot) {
    var title = dot.querySelector("title");
    if (title) title.remove();  // чтобы не дублировать системную подсказку
    dot.addEventListener("mouseenter", function (event) { show(dot, event); });
    dot.addEventListener("mousemove", place);
    dot.addEventListener("mouseleave", function () { tip.hidden = true; });
    dot.setAttribute("tabindex", "0");
    dot.addEventListener("focus", function (event) { show(dot, event); });
    dot.addEventListener("blur", function () { tip.hidden = true; });
  });
})();
</script>
""".strip()


def _share(count: int, total: int) -> str:
    """Доля от сыгранных COTD: '12,3 % дней'. Для нуля подписи нет."""
    if not count or not total:
        return ""
    percent = round(count / total * 100, 1)
    if percent <= 0:
        return ""
    return f"{_num(percent, 1)} % дней"


# Насколько «частота дивизиона» важнее его номера при сортировке: ключ =
# mode_div * SCALE - mode_div_count, поэтому при равном дивизионе чаще
# встречавшийся идёт раньше (1 (×98) -> 9902, 1 (×15) -> 9985).
MODE_SORT_SCALE = 10000


def _mode_sort_value(stats: Stats) -> float | None:
    """Ключ сортировки колонки «Частый дивизион»: дивизион, затем число повторов."""
    if stats.mode_div is None:
        return None
    return float(stats.mode_div * MODE_SORT_SCALE - stats.mode_div_count)


def _compare_metric(stats: Stats, key: str) -> float | None:
    """Числовое значение метрики для сортировки таблицы сравнения."""
    values = {
        "cotd": stats.total,
        "avg_div": stats.avg_div,
        "avg_rank": stats.avg_rank,
        "avg_top": stats.avg_top_pct,
        "avg_div_rank": stats.avg_div_rank,
        "mode_div": _mode_sort_value(stats),
        "median_rank": stats.median_rank,
        "median_div_rank": stats.median_div_rank,
        "median_top": stats.median_top_pct,
    }
    value = values.get(key)
    return None if value is None else float(value)


COMPARE_LABELS: dict[str, str] = {
    "avg_div": "Средний дивизион",
    "avg_rank": "Среднее место",
    "avg_top": "Средний топ по миру",
    "avg_div_rank": "Среднее место в раундах",
    "mode_div": "Самый частый дивизион",
    "median_rank": "Место (медиана)",
    "median_div_rank": "Раунды (медиана)",
    "median_top": "Топ по миру (медиана)",
}

COMPARE_SECTIONS: dict[str, tuple[str, ...]] = {
    "averages": ("avg_div", "avg_rank", "avg_top", "avg_div_rank"),
    "medians": ("mode_div", "median_rank", "median_div_rank", "median_top"),
    "all": (
        "avg_rank",
        "avg_top",
        "avg_div_rank",
        "mode_div",
        "median_rank",
        "median_div_rank",
        "median_top",
    ),
}

COMPARE_DEFAULT_SORT: dict[str, str] = {
    "averages": "avg_rank",
    "medians": "median_rank",
    "all": "avg_rank",
}


def _compare_cells(stats: Stats) -> dict[str, tuple[str, str]]:
    """Значения и тексты ячеек таблицы сравнения по ключам колонок."""
    mode_div = _place(stats.mode_div)
    if stats.mode_div is not None and stats.mode_div_count:
        mode_div = f"{_place(stats.mode_div)} (×{stats.mode_div_count})"
    return {
        "cotd": (_place(stats.total), _place(stats.total).replace(" ", "")),
        "avg_div": (
            _num(stats.avg_div, 1),
            f"{stats.avg_div:.4f}" if stats.avg_div is not None else "",
        ),
        "avg_rank": (_place(stats.avg_rank), _place(stats.avg_rank).replace(" ", "")),
        "avg_top": (
            _pct(stats.avg_top_pct),
            f"{stats.avg_top_pct or 0:.6f}" if stats.avg_top_pct is not None else "",
        ),
        "avg_div_rank": (
            _place(stats.avg_div_rank),
            _place(stats.avg_div_rank).replace(" ", ""),
        ),
        "mode_div": (
            mode_div,
            "" if stats.mode_div is None else f"{_mode_sort_value(stats):.0f}",
        ),
        "median_rank": (
            _place(stats.median_rank),
            _place(stats.median_rank).replace(" ", ""),
        ),
        "median_div_rank": (
            _place(stats.median_div_rank),
            _place(stats.median_div_rank).replace(" ", ""),
        ),
        "median_top": (
            _pct(stats.median_top_pct),
            f"{stats.median_top_pct or 0:.6f}" if stats.median_top_pct is not None else "",
        ),
    }


def render_compare(
    players: Sequence[tuple[str, Stats, bool]],
    *,
    sort_key: str | None = None,
    descending: bool = False,
    min_players: int = 2,
    section: str = "all",
) -> str:
    """HTML-таблица сравнения с сортировкой по клику на заголовок.

    ``section`` выбирает набор колонок: ``averages`` (средние), ``medians``
    (медианы) или ``all`` (всё вместе).
    ``sort_key``/``descending`` задают порядок при открытии страницы, дальше
    колонки можно пересортировать в браузере (инлайновый скрипт, без зависимостей).
    ``min_players`` — сколько игроков нужно, чтобы таблица имела смысл (на главной
    достаточно одного: по нику можно перейти на его страницу).
    """
    if len(players) < max(1, min_players):
        return ""

    columns_keys = COMPARE_SECTIONS.get(section, COMPARE_SECTIONS["all"])
    if sort_key is None:
        sort_key = COMPARE_DEFAULT_SORT.get(section, "avg_rank")
    if sort_key not in columns_keys:
        sort_key = COMPARE_DEFAULT_SORT.get(section, columns_keys[0])

    def sort_value(item: tuple[str, Stats, bool]) -> float:
        value = _compare_metric(item[1], sort_key)
        return float("inf") if value is None else value

    known = [item for item in players if _compare_metric(item[1], sort_key) is not None]
    unknown = [item for item in players if _compare_metric(item[1], sort_key) is None]
    ordered = sorted(known, key=sort_value, reverse=descending) + unknown

    rows: list[str] = []
    for index, (name, stats, is_current) in enumerate(ordered, start=1):
        marker = ' class="is-current"' if is_current else ""
        values = _compare_cells(stats)
        cells = [
            (
                "name",
                _player_cell(
                    name,
                    stats.flag,
                    stats.zone,
                    "" if is_current else stats.href,
                ),
                html.escape(name),
            ),
            ("cotd", *values["cotd"]),
        ]
        cells += [(key, *values[key]) for key in columns_keys]
        rows.append(
            f"<tr{marker}>"
            + _pos_cell(index)
            + "".join(_td(key, text, value) for key, text, value in cells)
            + "</tr>"
        )

    columns = [("name", "Игрок"), ("cotd", "COTD")]
    columns += [(key, COMPARE_LABELS[key]) for key in columns_keys]

    def th(key: str, label: str) -> str:
        classes = ["cell-name"] if key == "name" else []
        if key == sort_key:
            classes.append("is-sorted")
        css = f' class="{" ".join(classes)}"' if classes else ""
        return f'<th{css} data-key="{key}">{label}</th>'

    header = _pos_head() + "".join(th(key, label) for key, label in columns)
    return (
        f'<div class="table-wrap"><table class="table" data-sort="{sort_key}">'
        f"<thead><tr>{header}</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def render_partners(partners: Sequence[Partner]) -> str:
    """HTML-таблица «с кем чаще всего играл» — те же классы, что у сравнения."""
    if not partners:
        return ""

    rows: list[str] = []
    for index, partner in enumerate(partners, start=1):
        shared_value = str(partner.shared)
        share_text = _pct(partner.share) if partner.share is not None else DASH
        share_value = f"{partner.share:.6f}" if partner.share is not None else ""
        gap_text = _num(partner.avg_gap, 0) if partner.avg_gap is not None else DASH
        gap_value = f"{partner.avg_gap:.6f}" if partner.avg_gap is not None else ""
        cells = [
            (
                "name",
                _player_cell(partner.name, partner.flag, partner.zone, partner.href),
                html.escape(partner.name),
            ),
            ("shared", _place(partner.shared), shared_value),
            ("share", share_text, share_value),
            ("gap", gap_text, gap_value),
        ]
        rows.append(
            "<tr>"
            + _pos_cell(index)
            + "".join(_td(key, text, value) for key, text, value in cells)
            + "</tr>"
        )

    columns = [
        ("name", "Игрок"),
        ("shared", "Общих COTD"),
        ("share", "Доля моих COTD"),
        ("gap", "Ср. разница мест"),
    ]

    def th(key: str, label: str) -> str:
        classes = ["cell-name"] if key == "name" else []
        if key == "shared":
            classes.append("is-sorted")
        css = f' class="{" ".join(classes)}"' if classes else ""
        return f'<th{css} data-key="{key}">{label}</th>'

    header = _pos_head() + "".join(th(key, label) for key, label in columns)
    return (
        '<div class="table-wrap"><table class="table" data-sort="shared">'
        f"<thead><tr>{header}</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def render_html(
    stats: Stats,
    *,
    generated: str = "",
    source: str = "trackmania.io",
    partners_html: str = "",
    home: tuple[str, str, bool] | None = None,
    year_picker: str = "",
    assets: str = "",
) -> str:
    """Собирает HTML-страницу со статистикой.

    ``assets`` — префикс общих файлов сайта (``../assets/``): тогда CSS и JS
    подключаются отдельными файлами; пустая строка — всё встроено в страницу.
    """
    year_label = stats.year or "весь период"
    page_title = f"{stats.name} · COTD {year_label}"
    chart = _chart_svg(stats)
    # Подсказку про дивизионы показываем, только если часть дней без дивизиона.
    qualified_hint = ""
    if stats.qualified and stats.qualified != stats.total:
        qualified_hint = (
            f"из {stats.qualified} {_plural(stats.qualified, 'дня', 'дней', 'дней')} с дивизионом"
        )
    total_words = f"{stats.total} {_plural(stats.total, 'кубок', 'кубка', 'кубков')} дня"

    progress = stats.progress
    averages = "".join(
        [
            _card(
                "Средний дивизион",
                _num(stats.avg_div, 1),
                qualified_hint,
                delta_html=_delta_html(progress.get("avg_div"), prev_year=stats.prev_year),
            ),
            _card(
                "Среднее место",
                _place(stats.avg_rank),
                "в мире",
                delta_html=_delta_html(progress.get("avg_rank"), prev_year=stats.prev_year),
            ),
            _card(
                "Среднее место в раундах",
                _place(stats.avg_div_rank),
                "место в дивизионе",
                delta_html=_delta_html(progress.get("avg_div_rank"), prev_year=stats.prev_year),
            ),
            _card(
                "Средний топ по миру",
                _pct(stats.avg_top_pct),
                "доля игроков впереди",
                delta_html=_delta_html(
                    progress.get("avg_top_pct"), prev_year=stats.prev_year, percent=True
                ),
            ),
        ]
    )

    medians = "".join(
        [
            _card(
                "Самый частый дивизион",
                _place(stats.mode_div),
                f"{stats.mode_div_count} раз" if stats.mode_div_count else "нет данных",
                delta_html=_delta_html(progress.get("mode_div"), prev_year=stats.prev_year),
            ),
            _card(
                "Место (медиана)",
                _place(stats.median_rank),
                "в мире",
                delta_html=_delta_html(progress.get("median_rank"), prev_year=stats.prev_year),
            ),
            _card(
                "Раунды (медиана)",
                _place(stats.median_div_rank),
                "место в дивизионе",
                delta_html=_delta_html(progress.get("median_div_rank"), prev_year=stats.prev_year),
            ),
            _card(
                "Топ по миру (медиана)",
                _pct(stats.median_top_pct),
                "доля игроков впереди",
                delta_html=_delta_html(
                    progress.get("median_top_pct"), prev_year=stats.prev_year, percent=True
                ),
            ),
        ]
    )

    progress_note = ""
    if progress:
        progress_note = (
            f'<p class="note">Рядом с числом — разница с {html.escape(stats.prev_year)}: '
            '<span class="delta delta--good delta--inline">−N</span> зелёным — стало лучше '
            '(меньше), <span class="delta delta--bad delta--inline">+N</span> красным — хуже. '
            "Сравнение есть, "
            f"только если в обоих годах хотя бы {PROGRESS_MIN_STARTS} стартов.</p>"
        )
    elif stats.total >= PROGRESS_MIN_STARTS:
        progress_note = (
            '<p class="note">Сравнить не с чем: за предыдущий год нет данных '
            f"(нужно хотя бы {PROGRESS_MIN_STARTS} стартов).</p>"
        )

    results = "".join(
        [
            _card("Первые места", _place(stats.first), _share(stats.first, stats.total), "gold"),
            _card(
                "Вторые места", _place(stats.second), _share(stats.second, stats.total), "silver"
            ),
            _card("Третьи места", _place(stats.third), _share(stats.third, stats.total), "bronze"),
            _card("Топ-8", _place(stats.top8), _share(stats.top8, stats.total)),
            _card("Топ-32", _place(stats.top32), _share(stats.top32, stats.total)),
        ]
    )

    maps = _map_card("Лучшая карта", stats.best, "best") + _map_card(
        "Худшая карта", stats.worst, "worst"
    )

    if chart:
        chart_block = (
            '<div class="legend">'
            f'<span><i style="background:{WORLD_COLOR}"></i>место в мире</span>'
            '<span><i class="legend__dash"></i>тренд</span>'
            "</div>"
            f'<div class="chart">{chart}</div>'
            '<p class="note">Точки — дни с результатом, сплошная линия соединяет их, '
            "пунктир — тренд по этим дням. Ось Y логарифмическая: ниже — лучше. "
            "Шкала обрезана по 95-му перцентилю, чтобы редкие провальные дни не "
            "сжимали остальные: такие точки прижаты к верхней границе, точное место — "
            "в подсказке при наведении.</p>"
        )
    else:
        chart_block = '<p class="note">Недостаточно данных для графика.</p>'

    top_chart = _chart_top_svg(stats)
    if top_chart:
        top_chart_block = (
            '<div class="legend">'
            f'<span><i style="background:{WORLD_COLOR}"></i>топ по миру</span>'
            '<span><i class="legend__dash"></i>тренд</span>'
            "</div>"
            f'<div class="chart">{top_chart}</div>'
            '<p class="note">Топ по миру — доля игроков впереди в процентах '
            "(меньше — лучше). Сплошная линия соединяет дни, пунктир — тренд. "
            "Шкала обрезана по 95-му перцентилю: редкие провалы прижаты к верхней "
            "границе, точное значение — в подсказке при наведении.</p>"
        )
    else:
        top_chart_block = '<p class="note">Недостаточно данных для графика.</p>'

    div_chart = _chart_div_svg(stats)
    if div_chart:
        div_chart_block = (
            '<div class="legend">'
            f'<span><i style="background:{WORLD_COLOR}"></i>дивизион дня</span>'
            "<span>ниже — ближе к 1-му дивизиону</span>"
            "</div>"
            f'<div class="chart">{div_chart}</div>'
            '<p class="note">Один столбик — один день COTD: чем ниже столбик, тем ближе '
            "дивизион к 1-му (ниже — сильнее). Шкала обрезана по 95-му перцентилю, чтобы "
            "редкие слабые дни не сжимали остальные: такие дни прижаты к верху, точный "
            "дивизион — в подсказке при наведении. У дней без дивизиона столбика нет.</p>"
        )
    else:
        div_chart_block = '<p class="note">Недостаточно данных для графика.</p>'

    empty_banner = ""
    if not stats.total:
        empty_banner = (
            '<div class="empty">За выбранный период не нашлось ни одного кубка дня. '
            "Проверь год, id игрока или запусти сбор без фильтров.</div>"
        )

    hero_flag = _flag_img(stats.flag, stats.zone, css="flag flag--hero")

    footer = "Сгенерировано"
    if generated:
        footer += f" · {html.escape(generated)}"
    footer += (
        f' · данные: <a href="https://{html.escape(source)}" '
        f'rel="noopener noreferrer">{html.escape(source)}</a>'
    )

    partners_section = ""
    if partners_html:
        partners_section = (
            "  <section>\n"
            "    <h2>С кем чаще всего играл</h2>\n"
            f"    {partners_html}\n"
            '    <p class="note">Совпали день и дивизион — значит вы ехали один и тот же '
            "заезд COTD. «Доля моих COTD» — сколько моих дней из этого списка пришлось "
            "на общий дивизион, «Ср. разница мест» — насколько в среднем различались "
            "наши места в мире. Нажми на заголовок колонки, чтобы отсортировать таблицу.</p>\n"
            "  </section>\n\n"
        )

    home_html = render_home_link(home)
    hero_bar_items = f"{home_html}{year_picker}"
    hero_bar = f'    <div class="hero__bar">{hero_bar_items}</div>\n' if hero_bar_items else ""
    body = f"""  <header class="hero hero--player">
{hero_bar}    <div class="hero__top">
      <p class="eyebrow">Trackmania · Cup of the Day · {html.escape(year_label)}</p>
    </div>
    <div class="hero__body">
      <div class="hero__main">
        <h1>{hero_flag}{html.escape(stats.name)}</h1>
        <p class="sub">{html.escape(total_words)}</p>
      </div>
      <div class="hero__side">
        <div class="badges">
          <div class="badge" title="все COTD за период">
            <b>{html.escape(str(stats.total))}</b><small>COTD</small>
          </div>
          <div class="badge badge--played" title="старты, где игрок реально ехал: {html.escape(str(stats.unplayed))} раз зашёл, но не играл">
            <b>{html.escape(str(stats.played))}</b><small>сыграно</small>
          </div>
        </div>
      </div>
    </div>
  </header>

  {empty_banner}

  <section>
    <h2>Средние значения</h2>
    <div class="grid grid--4">{averages}</div>
    {progress_note}
  </section>

  <section>
    <h2>Медианы</h2>
    <div class="grid grid--4">{medians}</div>
  </section>

  <section>
    <h2>Результаты</h2>
    <div class="grid grid--5">{results}</div>
  </section>

  <section>
    <h2>Карты</h2>
    <div class="maps">{maps}</div>
  </section>

{partners_section}  <section>
    <h2>Дивизионы по дням · {html.escape(year_label)}</h2>
    {div_chart_block}
  </section>

  <section>
    <h2>Места по дням · {html.escape(year_label)}</h2>
    {chart_block}
  </section>

  <section>
    <h2>Топ по миру по дням · {html.escape(year_label)}</h2>
    {top_chart_block}
  </section>
"""
    return _document(
        title=page_title,
        content=body,
        footer=footer,
        assets=assets,
    )


RANKS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("first", "1-е"),
    ("second", "2-е"),
    ("third", "3-е"),
    ("top3", "Топ-3"),
    ("top8", "Топ-8"),
    ("top32", "Топ-32"),
)

RANKS_DESCRIPTIONS: dict[str, str] = {
    "first": "первые места в раундах",
    "second": "вторые места в раундах",
    "third": "третьи места в раундах",
    "top3": "попадания в топ-3 раундов",
    "top8": "попадания в топ-8 раундов",
    "top32": "попадания в топ-32 раундов",
}


def render_ranks_table(
    players: Sequence[tuple[str, Stats, bool]],
    *,
    sort_key: str = "first",
    descending: bool = True,
) -> str:
    """Таблица мест в раундах: сколько раз игрок был 1/2/3, входил в топ-8 и топ-32."""
    if not players:
        return ""
    key = sort_key if sort_key in RANKS_DESCRIPTIONS else "first"

    def metric(stats: Stats) -> int:
        value = getattr(stats, key, 0)
        return int(value) if isinstance(value, (int, float)) else 0

    ordered = sorted(players, key=lambda item: -metric(item[1]) if descending else metric(item[1]))

    rows: list[str] = []
    for index, (name, stats, is_current) in enumerate(ordered, start=1):
        cells = [
            (
                "name",
                _player_cell(name, stats.flag, stats.zone, "" if is_current else stats.href),
                html.escape(name),
            ),
            ("cotd", _place(stats.total), _place(stats.total).replace(" ", "")),
        ]
        for column, _label in RANKS_COLUMNS:
            value = getattr(stats, column, 0) or 0
            share = _share(int(value), stats.total)
            title = f' title="{html.escape(share)}"' if share else ""
            cells.append((column, f"<span{title}>{_place(value)}</span>", str(int(value))))
        rows.append(
            "<tr>"
            + _pos_cell(index)
            + "".join(_td(key, text, value) for key, text, value in cells)
            + "</tr>"
        )

    columns = [("name", "Игрок"), ("cotd", "COTD"), *RANKS_COLUMNS]

    def th(column: str, label: str) -> str:
        classes = ["cell-name"] if column == "name" else []
        if column == sort_key:
            classes.append("is-sorted")
        css = f' class="{" ".join(classes)}"' if classes else ""
        order = ' data-order="desc"' if column == sort_key and descending else ""
        return f'<th{css} data-key="{column}"{order}>{label}</th>'

    header = _pos_head() + "".join(th(column, label) for column, label in columns)
    return (
        f'<div class="table-wrap"><table class="table" data-sort="{html.escape(key)}">'
        f"<thead><tr>{header}</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


MIN_COTD_DEFAULT = 10
MIN_COTD_OPTIONS: tuple[int, ...] = (10, 25, 50, 100, 0)


def render_min_cotd(default: int = MIN_COTD_DEFAULT) -> str:
    """Маленький фильтр «от N COTD» рядом с вкладками таблиц.

    Прячет строки таблиц, где COTD меньше порога (``0`` — показать всех).
    Работает на клиенте, поэтому данные в HTML остаются полными.
    """
    options: list[str] = []
    for value in MIN_COTD_OPTIONS:
        label = "все" if value == 0 else f"от {value}"
        active = " is-current" if value == default else ""
        options.append(
            f'<li><button type="button" class="min-cotd__option{active}" '
            f'data-min="{value}">{label}</button></li>'
        )
    current = "все" if default == 0 else f"от {default}"
    return (
        f'<details class="min-cotd" data-min-cotd="{default}">'
        f"<summary><span data-min-label>{current}</span> <b>COTD</b></summary>"
        f'<ul class="min-cotd__list">{"".join(options)}</ul>'
        "</details>"
    )


MIN_COTD_SCRIPT = """
<script>
(function () {
  var root = document.querySelector("[data-min-cotd]");
  if (!root) return;
  var label = root.querySelector("[data-min-label]");
  var buttons = root.querySelectorAll("[data-min]");
  // Номера строк — от видимого порядка: скрытые строки номер не занимают.
  function renumber(table) {
    if (!table.tBodies.length) return;
    var n = 0;
    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      var cell = row.querySelector(".cell-pos");
      if (!cell) return;
      if (row.hidden) { cell.textContent = ""; return; }
      n += 1;
      cell.textContent = String(n);
    });
  }
  function apply(min) {
    document.querySelectorAll("table.table").forEach(function (table) {
      if (!table.tBodies.length) return;
      Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
        var cell = row.querySelector('[data-key="cotd"]');
        var value = cell ? parseInt(cell.getAttribute("data-value"), 10) : 0;
        row.hidden = min > 0 && (isNaN(value) || value < min);
      });
      renumber(table);
    });
    buttons.forEach(function (button) {
      var value = parseInt(button.getAttribute("data-min"), 10);
      button.classList.toggle("is-current", value === min);
    });
    if (label) label.textContent = min > 0 ? "от " + min : "все";
    root.open = false;
  }
  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      apply(parseInt(button.getAttribute("data-min"), 10));
    });
  });
  var initial = parseInt(root.getAttribute("data-min-cotd"), 10);
  apply(isNaN(initial) ? 10 : initial);
})();
</script>
""".strip()


def render_compare_page(
    players: Sequence[tuple[str, Stats, bool]],
    *,
    home: tuple[str, str, bool] | None = None,
    year_picker: str = "",
    generated: str = "",
    source: str = "trackmania.io",
    year_label: str = "",
    sort_key: str = "avg_rank",
    descending: bool = False,
    assets: str = "",
) -> str:
    """Отдельная страница-обзор: таблица сравнения всех игроков."""
    averages_table = render_compare(
        players,
        sort_key=sort_key,
        descending=descending,
        min_players=1,
        section="averages",
    )
    medians_table = render_compare(players, min_players=1, section="medians")
    ranks_table = render_ranks_table(players)
    footer = "Сгенерировано"
    if generated:
        footer += f" · {html.escape(generated)}"
    footer += (
        f' · данные: <a href="https://{html.escape(source)}" '
        f'rel="noopener noreferrer">{html.escape(source)}</a>'
    )
    subtitle = html.escape(year_label) if year_label else "весь период"
    min_cotd = render_min_cotd()
    home_html = render_home_link(home)
    hero_bar = f'    <div class="hero__bar">{home_html}</div>\n' if home_html else ""
    body = f"""  <header class="hero">
{hero_bar}    <div class="hero__top">
      <p class="eyebrow">Trackmania · Cup of the Day · {subtitle}</p>
      {year_picker}
    </div>
    <div class="hero__body">
      <div class="hero__main">
        <h1>Таблица игроков</h1>
        <p class="sub">{len(players)} {_plural(len(players), "игрок", "игрока", "игроков")} в кеше</p>
      </div>
    </div>
  </header>

  <section>
    <h2>Сравнение игроков</h2>
    <div class="table-controls">
      <div class="tabs" role="tablist">
        <button type="button" class="tab is-active" data-tab="averages"
                aria-selected="true">Средние показатели</button>
        <button type="button" class="tab" data-tab="medians"
                aria-selected="false">Медианные показатели</button>
        <button type="button" class="tab" data-tab="ranks"
                aria-selected="false">Места в раундах</button>
      </div>
      {min_cotd}
    </div>
    <div class="panel" data-panel="averages">
      {averages_table or '<p class="note">Пока нет данных: собери хотя бы одного игрока.</p>'}
    </div>
    <div class="panel" data-panel="medians" hidden>
      {medians_table or '<p class="note">Пока нет данных: собери хотя бы одного игрока.</p>'}
    </div>
    <div class="panel" data-panel="ranks" hidden>
      {ranks_table or '<p class="note">Пока нет данных: собери хотя бы одного игрока.</p>'}
    </div>
    <p class="note">Нажми на заголовок колонки, чтобы отсортировать таблицу — каждая
    сортируется независимо. «Средние» — средние значения за период, «Медианные» —
    медианы и самый частый дивизион, «Места в раундах» — сколько раз игрок занимал
    1-е, 2-е и 3-е место, попадал в топ-3, топ-8 и топ-32 (в подсказке у числа —
    доля от сыгранных COTD).</p>
  </section>
"""
    return _document(
        title=f"Таблица игроков · COTD {year_label}".strip(),
        content=body,
        footer=footer,
        assets=assets,
    )
