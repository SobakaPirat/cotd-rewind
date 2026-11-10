#!/usr/bin/env python3
"""Сбор таблицы кубков дня (COTD) игрока с trackmania.io.

Заменяет прежний marimo-ноутбук: набор колонок тот же, но сбор устойчивее —
есть таймауты и ретраи (429/5xx), остановка по дате вместо жёсткого "2025",
защита от деления на ноль, от зацикливания пагинации и от мусора в ответах API.

Использование:
    python main.py <tm.io id> -s 2025-01-01
    python main.py <tm.io id> -s 2025-01-01 -o cotd.tsv
    python main.py --player-id <tm.io id> -f csv --skip-unqualified > cotd.csv

Идентификатор игрока можно передать позиционно, через --player-id или
через переменную окружения TMIO_PLAYER_ID.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import json
import logging
import os
import random
import re
import shutil
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, TextIO
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import report

API_BASE = "https://trackmania.io/api"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_PAGES = 200
RETRY_STATUSES = (429, 500, 502, 503, 504)
# Подставляется, если свой User-Agent не задан ни в окружении, ни в local_settings.py.
DEFAULT_USER_AGENT = "cotd-recap (trackmania.io COTD stats)"


def _load_local_user_agent() -> str:
    """User-Agent из local_settings.py.

    Файл лежит в .gitignore: trackmania.io просит указывать контакт, но личные
    данные не должны попадать в репозиторий. Образец — local_settings.example.py.
    """
    try:
        from local_settings import USER_AGENT as value
    except ImportError:
        return ""
    return str(value).strip()


USER_AGENT = (
    os.environ.get("TMIO_USER_AGENT", "").strip() or _load_local_user_agent() or DEFAULT_USER_AGENT
)
# Где trackmania.io держит список записей: 'cotds' (кубки дня) и 'cups' (кубки).
# Реальный API для /player/{id}/cotd/{page} отдаёт именно 'cups'.
LIST_FIELDS = ("cotds", "cups")
# Кандидаты имён полей внутри записи: между эндпоинтами они различаются.
DATE_FIELDS = ("timestamp", "date", "startdate", "startDate")
NAME_FIELDS = ("mapname", "mapName", "map_name", "name")
DIV_FIELDS = ("div", "division", "divname", "divName")
DIV_RANK_FIELDS = ("divrank", "divRank", "divisionrank", "divisionRank")
RANK_FIELDS = ("rank", "position", "globalrank", "globalRank")
PLAYERS_FIELDS = ("totalplayers", "totalPlayers", "playercount")

# Порядок колонок фиксирован явно: раньше он зависел от порядка вставки в dict,
# и любая правка кода молча ломала TSV.
COLUMNS: tuple[str, ...] = (
    "map_date",
    "map_name",
    "div",
    "div_rank",
    "rank",
    "top",
    "total_players",
)

LOG = logging.getLogger("cotd-recap")

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Коды форматирования Trackmania: цвет ($f00, $abc — 3 hex) либо одиночный
# код ($i, $o, $n, $w, $s, $g, $z, $t, $m, $<, $>, $l). Литеральный доллар
# в именах карт пишется как "$$" — его сохраняем как "$".
_MAP_STYLE_RE = re.compile(r"\$(?:[0-9a-f]{3}|[iownmtsgz<>l])", re.IGNORECASE)
_DOLLAR_STUB = "\x00"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class CotdError(RuntimeError):
    """Базовая ошибка сбора."""


class ApiError(CotdError):
    """API ответил не тем, что ожидалось."""


class PlayerNotFound(CotdError):
    """Игрок не найден (HTTP 404)."""


class RateLimited(CotdError):
    """API продолжает отдавать 429 даже после ретраев."""


@dataclass
class CollectStats:
    """Счётчики сбора: без них непонятно, почему таблица пустая."""

    pages: int = 0
    fetched: int = 0
    no_date: int = 0
    before_since: int = 0
    after_until: int = 0
    unqualified: int = 0
    filtered_by_name: int = 0
    from_cache: bool = False
    examples: list[str] = field(default_factory=list)

    def rejected(self) -> int:
        return (
            self.no_date
            + self.before_since
            + self.after_until
            + self.unqualified
            + self.filtered_by_name
        )

    def summary(self) -> str:
        parts = [f"страниц: {self.pages}", f"записей из API: {self.fetched}"]
        if self.from_cache:
            parts.append("источник: кеш")
        if self.before_since:
            parts.append(f"отсечено по --since: {self.before_since}")
        if self.after_until:
            parts.append(f"отсечено по --until: {self.after_until}")
        if self.no_date:
            parts.append(f"без распознаваемой даты: {self.no_date}")
        if self.unqualified:
            parts.append(f"без дивизиона: {self.unqualified}")
        if self.filtered_by_name:
            parts.append(f"не COTD по названию: {self.filtered_by_name}")
        return ", ".join(parts)


# ---------------------------------------------------------------- нормализация


def as_int(value: Any) -> int | None:
    """Приводит значение из JSON к int, не падая на None/строках/мусоре."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except (ValueError, OverflowError):
                return None
    return None


def parse_date(value: Any) -> date | None:
    """Достаёт дату из timestamp: ISO-строка, unix-время (int/float/строка)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        # На случай unix-времени в миллисекундах.
        if abs(seconds) > 1e11:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        match = _DATE_RE.search(value)
        if match:
            try:
                return date.fromisoformat(match.group(0))
            except ValueError:
                return None
        text = value.strip()
        if text.lstrip("-").isdigit():
            return parse_date(int(text))
    return None


def clean_map_name(value: Any) -> str:
    """Убирает $fff/$i-разметку и приводит имя карты к одной строке."""
    if value is None:
        return ""
    # "$$" — это литеральный доллар, а не код: прячем его от чистки.
    text = str(value).replace("$$", _DOLLAR_STUB)
    text = _MAP_STYLE_RE.sub("", text)
    text = text.replace(_DOLLAR_STUB, "$")
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return text.strip()


def _pick(entry: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """Первое не-None значение среди перечисленных ключей."""
    for key in keys:
        value = entry.get(key)
        if value is not None:
            return value
    return default


def build_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Одна запись API -> строка таблицы с фиксированными типами.

    Имена полей в trackmania.io различаются между эндпоинтами ('cotds' и 'cups'),
    поэтому у каждого значения есть несколько кандидатов.
    """
    map_date = parse_date(_pick(entry, *DATE_FIELDS))
    total_players = as_int(_pick(entry, *PLAYERS_FIELDS))
    rank = as_int(_pick(entry, *RANK_FIELDS))
    div = as_int(_pick(entry, *DIV_FIELDS))
    div_rank = as_int(_pick(entry, *DIV_RANK_FIELDS))

    map_name = _pick(entry, "mapname", "mapName", "map_name")
    map_field = entry.get("map")
    if isinstance(map_field, Mapping):
        map_name = _pick(map_field, "name", "mapname", "mapName", default=map_name)
    if map_name is None:
        map_name = entry.get("name")

    # 0 в divrank означает "без места в дивизионе" -> None, а не "" (пустая
    # строка ломала сортировку и превращала колонку в текстовую).
    if div_rank == 0:
        div_rank = None

    # Раньше здесь было rank / totalplayers без защиты: totalplayers == 0
    # роняло весь сбор ZeroDivisionError.
    top = round(rank / total_players, 6) if rank is not None and total_players else None

    return {
        "map_date": map_date.isoformat() if map_date else "",
        "map_name": clean_map_name(map_name),
        "map_uid": str(_pick(entry, "mapuid", "mapUid", "uid") or ""),
        "div": div,
        "div_rank": div_rank,
        "rank": rank,
        "top": top,
        "total_players": total_players,
    }


# ------------------------------------------------------------------ сбор данных


def build_session(*, retries: int = 6, backoff: float = 2.0) -> requests.Session:
    """Сессия с User-Agent и ретраями на 429/5xx и сетевые сбои."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        backoff_max=60.0,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        # Ошибки разбираем сами, чтобы отдавать понятные сообщения.
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_cotd_page(
    session: requests.Session,
    player_id: str,
    page: int,
    *,
    base_url: str = API_BASE,
    include_reruns: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Одна страница /player/{id}/cotd/{page} -> список записей."""
    url = f"{base_url.rstrip('/')}/player/{quote(str(player_id).strip(), safe='')}/cotd/{page}"
    params = {
        "includeReruns": "true" if include_reruns else "false",
        "order": "dateDesc",
    }

    try:
        response = session.get(
            url,
            params=params,
            timeout=(min(10.0, timeout), timeout),
        )
    except requests.RequestException as exc:
        raise ApiError(f"не удалось запросить {url}: {exc}") from exc

    if response.status_code == 404:
        raise PlayerNotFound(f"игрок {player_id!r} не найден (HTTP 404)")
    if response.status_code == 429:
        raise RateLimited(
            "trackmania.io ограничил частоту запросов (HTTP 429), "
            "ретраи не помогли — увеличь --delay"
        )
    if response.status_code >= 400:
        raise ApiError(f"HTTP {response.status_code} при запросе {url}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiError(
            f"ответ не является JSON (HTTP {response.status_code}): {response.text[:200]!r}"
        ) from exc

    return _extract_entries(payload, page), payload


def _describe_payload(payload: Any) -> str:
    """Короткое описание ответа — чтобы понять, почему он пустой."""
    if isinstance(payload, Mapping):
        keys = sorted(str(key) for key in payload)
        extra = ""
        for key in ("total", "count", "totalresults", "totalResults", "totalcount"):
            if key in payload:
                extra = f", {key}={payload[key]!r}"
                break
        return f"ключи ответа: {keys}{extra}"
    if isinstance(payload, list):
        return f"ответ — список длиной {len(payload)}"
    return f"ответ типа {type(payload).__name__}"


@dataclass
class PlayerProfile:
    """Что берём из /player/{id}: ник и зона (флаг — как на trackmania.io)."""

    name: str = ""
    zone: str = ""
    flag: str = ""
    # Флаги зон от ближайшей к миру: город, регион, страна, континент, мир.
    # У части зон (например, у Мюнхена) своего файла флага нет — тогда берём
    # ближайший родительский, как это делает сам trackmania.io.
    flag_chain: tuple[str, ...] = ()


def _zone_node(payload: Any) -> Mapping[str, Any] | None:
    """Зона игрока из ответа профиля: ``trophies.zone``, иначе ``zone``."""
    trophies = payload.get("trophies") if isinstance(payload, Mapping) else None
    zone: Any = None
    if isinstance(trophies, Mapping):
        zone = trophies.get("zone")
    if not isinstance(zone, Mapping) and isinstance(payload, Mapping):
        zone = payload.get("zone")
    return zone if isinstance(zone, Mapping) else None


def _zone_flag_chain(payload: Any) -> list[str]:
    """Флаги зон игрока от ближайшей к миру (без повторов).

    trackmania.io хранит имя файла флага у каждой зоны, но картинка есть не у
    всех: у города Мюнхен ``flag`` = ``Munchen``, а файла ``Munchen.jpg`` на
    сайте нет — он показывает флаг Баварии. Поэтому держим всю цепочку и при
    скачивании пробуем по порядку.
    """
    node = _zone_node(payload)
    chain: list[str] = []
    depth = 0
    while isinstance(node, Mapping) and depth < 8:
        flag = node.get("flag")
        if isinstance(flag, str) and flag.strip():
            name = flag.strip()
            if name not in chain:
                chain.append(name)
        node = node.get("parent")
        depth += 1
    return chain


def _player_zone(payload: Any) -> tuple[str, str]:
    """Название и флаг зоны игрока.

    trackmania.io показывает флаг ближайшей зоны, у которой он задан, поэтому
    при пустом ``flag`` поднимаемся по ``parent``; полная цепочка флагов —
    в :func:`_zone_flag_chain`.
    """
    zone = _zone_node(payload)
    if zone is None:
        return "", ""
    name = str(zone.get("name") or "").strip()
    chain = _zone_flag_chain(payload)
    return name, (chain[0] if chain else "")


def fetch_player_profile(
    session: requests.Session,
    player_id: str,
    *,
    base_url: str = API_BASE,
    timeout: float = DEFAULT_TIMEOUT,
) -> PlayerProfile:
    """Ник и зона игрока из /player/{id} (пустой профиль при неудаче)."""
    url = f"{base_url.rstrip('/')}/player/{quote(str(player_id).strip(), safe='')}"
    try:
        response = session.get(url, timeout=(min(10.0, timeout), timeout))
    except requests.RequestException as exc:
        LOG.debug("профиль игрока недоступен: %s", exc)
        return PlayerProfile()
    if response.status_code >= 400:
        LOG.debug("профиль игрока недоступен: HTTP %s", response.status_code)
        return PlayerProfile()
    try:
        payload = response.json()
    except ValueError:
        LOG.debug("профиль игрока: ответ не JSON")
        return PlayerProfile()

    name = _search_name(payload)
    zone, flag = _player_zone(payload)
    chain = tuple(_zone_flag_chain(payload))
    if name:
        LOG.debug("ник из профиля: %r (зона: %s, флаг: %s)", name, zone or "?", flag or "?")
    return PlayerProfile(name=name, zone=zone, flag=flag, flag_chain=chain)


def fetch_player_name(
    session: requests.Session,
    player_id: str,
    *,
    base_url: str = API_BASE,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Только ник игрока (совместимость с прежним API)."""
    return fetch_player_profile(session, player_id, base_url=base_url, timeout=timeout).name


def fetch_flag_data(
    session: requests.Session,
    flag: str,
    *,
    base_url: str = API_BASE,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = 400 * 1024,
) -> str:
    """Скачивает флаг зоны с trackmania.io и отдаёт data URI (пустая строка при неудаче).

    Картинка встраивается в страницу как base64, чтобы сайт оставался
    самодостаточным и не зависел от внешних CDN.
    """
    if not flag:
        return ""
    site = base_url.rstrip("/").removesuffix("/api")
    url = f"{site}/img/flags/{quote(flag.strip(), safe='')}.jpg"
    try:
        response = session.get(url, timeout=(min(10.0, timeout), timeout))
    except requests.RequestException as exc:
        LOG.debug("флаг %s недоступен: %s", flag, exc)
        return ""
    if response.status_code >= 400:
        LOG.debug("флаг %s недоступен: HTTP %s", flag, response.status_code)
        return ""

    ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not ctype.startswith("image/"):
        LOG.debug("флаг %s: пришёл не image (%s)", flag, ctype or "?")
        return ""
    data = response.content
    if not data or len(data) > max_bytes:
        LOG.debug("флаг %s: подозрительный размер (%s байт)", flag, len(data))
        return ""
    return f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"


def fetch_zone_flag(
    session: requests.Session,
    flags: Sequence[str],
    *,
    base_url: str = API_BASE,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[str, str]:
    """Первый доступный флаг из цепочки зон: ``(имя файла, data URI)``.

    У части зон trackmania.io не отдаёт картинку (например, у города Мюнхен:
    ``Munchen.jpg`` отвечает страницей сайта вместо JPEG). Тогда берём флаг
    ближайшей родительской зоны — так же поступает и сам сайт.
    """
    seen: set[str] = set()
    for flag in flags:
        name = (flag or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        data = fetch_flag_data(session, name, base_url=base_url, timeout=timeout)
        if data:
            return name, data
    return "", ""


def _search_name(payload: Any, depth: int = 0) -> str:
    """Ищет ник в ответе профиля, не забираясь слишком глубоко."""
    if depth > 1 or not isinstance(payload, Mapping):
        return ""
    for key in ("name", "nickname", "playername", "playerName", "displayname"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            nested = _search_name(value, depth + 1)
            if nested:
                return nested
    return ""


def _extract_entries(payload: Any, page: int) -> list[dict[str, Any]]:
    """Достаёт список записей из ответа, не доверяя его структуре.

    Список ищется в известных полях (``LIST_FIELDS``), затем — как единственный
    список в ответе. Предупреждаем только на первой странице, чтобы не спамить.
    """
    if isinstance(payload, list):
        entries: Any = payload
    elif isinstance(payload, Mapping):
        if payload.get("error"):
            raise ApiError(f"API вернул ошибку: {payload['error']!r}")

        entries = None
        for name in LIST_FIELDS:
            value = payload.get(name)
            if isinstance(value, list):
                entries = value
                if name != LIST_FIELDS[0]:
                    notifier = LOG.info if page == 0 else LOG.debug
                    notifier("список записей лежит в поле %r (не %r)", name, LIST_FIELDS[0])
                break

        if entries is None:
            # Поле могли переименовать: берём единственный список в ответе.
            lists = {key: value for key, value in payload.items() if isinstance(value, list)}
            if len(lists) == 1:
                name, entries = next(iter(lists.items()))
                notifier = LOG.warning if page == 0 else LOG.debug
                notifier("неизвестное поле со списком записей: %r (страница %d)", name, page)
            else:
                if page == 0:
                    LOG.warning(
                        "в ответе не найдено однозначного списка записей: %s",
                        _describe_payload(payload),
                    )
                entries = []
    else:
        raise ApiError(f"неожиданный тип ответа на странице {page}: {type(payload).__name__}")

    if not isinstance(entries, list):
        raise ApiError(f"список записей на странице {page} не список: {type(entries).__name__}")
    return [entry for entry in entries if isinstance(entry, Mapping)]


def _entry_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    """Ключ для отсева повторов, если API проигнорирует номер страницы."""
    return (
        _pick(entry, *DATE_FIELDS),
        _pick(entry, *NAME_FIELDS),
        _pick(entry, *DIV_FIELDS),
        _pick(entry, *RANK_FIELDS),
    )


def iter_player_cotds(
    session: requests.Session,
    player_id: str,
    *,
    base_url: str = API_BASE,
    include_reruns: bool = False,
    delay: float = 1.5,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: float = DEFAULT_TIMEOUT,
    dump: TextIO | None = None,
    stats: CollectStats | None = None,
) -> Iterator[dict[str, Any]]:
    """Идёт по страницам (dateDesc), пока не кончится история или --max-pages."""
    seen: set[tuple[Any, ...]] = set()

    for page in range(max_pages):
        entries, payload = fetch_cotd_page(
            session,
            player_id,
            page,
            base_url=base_url,
            include_reruns=include_reruns,
            timeout=timeout,
        )
        if stats is not None:
            stats.pages += 1
        if dump is not None:
            dumped = {"page": page, "url": base_url, "payload": payload}
            dump.write(json.dumps(dumped, ensure_ascii=False, default=str) + "\n")
            dump.flush()

        if not entries:
            if page == 0:
                LOG.warning(
                    "на первой странице 0 записей (%s) — проверь id игрока и параметры запроса",
                    _describe_payload(payload),
                )
            else:
                LOG.debug("страница %d пустая — история закончилась", page)
            return

        if page == 0:
            LOG.debug("ключи первой записи: %s", sorted(str(key) for key in entries[0]))
            LOG.debug(
                "пример записи: %s",
                json.dumps(entries[0], ensure_ascii=False, default=str)[:600],
            )

        fresh: list[dict[str, Any]] = []
        for entry in entries:
            key = _entry_key(entry)
            if key in seen:
                continue
            seen.add(key)
            fresh.append(entry)

        if not fresh:
            LOG.warning(
                "страница %d не дала новых записей — останавливаюсь (API игнорирует пагинацию?)",
                page,
            )
            return

        LOG.debug("страница %d: %d записей", page, len(fresh))
        yield from fresh

        if delay > 0:
            # Джиттер, чтобы не бить API ровным ритмом: trackmania.io отдаёт 429
            # на слишком частые запросы.
            time.sleep(delay * random.uniform(0.8, 1.3))

    LOG.warning("достигнут лимит --max-pages=%d: история может быть неполной", max_pages)


def collect_rows(
    session: requests.Session,
    player_id: str,
    *,
    since: date | None = None,
    until: date | None = None,
    skip_unqualified: bool = False,
    cup_filter: str | None = None,
    stats: CollectStats | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Собирает строки таблицы за период [since; until] включительно.

    Если передан ``stats``, он заполняется счётчиками (сколько записей пришло
    и сколько отсеяно на каком шаге) — это нужно для внятной диагностики
    пустого результата.
    """
    rows: list[dict[str, Any]] = []
    counters = stats if stats is not None else CollectStats()

    for entry in iter_player_cotds(session, player_id, stats=counters, **kwargs):
        counters.fetched += 1
        map_date = parse_date(_pick(entry, *DATE_FIELDS))
        if map_date is None:
            counters.no_date += 1
            if len(counters.examples) < 3:
                counters.examples.append(
                    f"date={_pick(entry, *DATE_FIELDS)!r}, "
                    f"name={entry.get('name')!r}, "
                    f"keys={sorted(str(key) for key in entry)[:8]}"
                )
            continue

        # Лента идёт от новых к старым: как только вышли за нижнюю границу,
        # дальше листать бессмысленно. Раньше вместо этого проверялось
        # наличие подстроки "2025" в названии карты.
        if since is not None and map_date < since:
            counters.before_since += 1
            LOG.info("достигнута нижняя граница %s — сбор закончен", since)
            break
        if until is not None and map_date > until:
            counters.after_until += 1
            continue

        if cup_filter:
            cup_name = str(_pick(entry, "name", "cupname", "campaignname") or "")
            if cup_filter.lower() not in cup_name.lower():
                counters.filtered_by_name += 1
                continue

        row = build_row(entry)
        if skip_unqualified and not row["div"]:
            counters.unqualified += 1
            continue
        rows.append(row)

    if counters.no_date:
        LOG.warning(
            "пропущено записей без распознаваемой даты: %d; примеры: %s",
            counters.no_date,
            " | ".join(counters.examples),
        )

    return rows


def guess_year(rows: Sequence[Mapping[str, Any]]) -> str:
    """Год для подписи страницы: единственный год или диапазон из данных."""
    years = sorted(
        {
            str(row.get("map_date"))[:4]
            for row in rows
            if isinstance(row.get("map_date"), str) and len(str(row.get("map_date"))) >= 4
        }
    )
    if not years:
        return str(datetime.now().year)
    if len(years) == 1:
        return years[0]
    return f"{years[0]}–{years[-1]}"


# ------------------------------------------------------------------------ вывод


def format_cell(value: Any) -> str:
    """Значение для TSV/CSV: None -> пусто, float -> фиксированные 6 знаков."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def write_table(
    rows: Sequence[Mapping[str, Any]],
    out: TextIO,
    *,
    delimiter: str = "\t",
    header: bool = True,
) -> None:
    """Пишет таблицу через csv.writer: сам экранирует разделители и кавычки."""
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
    if header:
        writer.writerow(list(COLUMNS))
    for row in rows:
        writer.writerow([format_cell(row.get(column)) for column in COLUMNS])


# ------------------------------------------------------------------------- кеш

CACHE_VERSION = 1


def cache_path(cache_dir: str, player_id: str) -> str:
    """Путь к файлу кеша игрока (id может быть uuid — чистим под имя файла)."""
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", str(player_id).strip()) or "player"
    return os.path.join(cache_dir, f"{safe}.json")


def load_cache(
    cache_dir: str,
    player_id: str,
    *,
    max_age_hours: float = 24.0,
    only_cotd: bool = False,
    since: date | None = None,
) -> dict[str, Any] | None:
    """Читает кеш игрока. Возвращает None, если кеша нет или он не подходит."""
    path = cache_path(cache_dir, player_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        LOG.warning("кеш %s не читается (%s) — собираю данные заново", path, exc)
        return None

    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        LOG.warning("кеш %s старого формата — собираю данные заново", path)
        return None

    if bool(payload.get("only_cotd")) != bool(only_cotd):
        LOG.info("кеш %s собран с другим --only-cotd — собираю данные заново", path)
        return None

    # Кеш может быть собран с нижней границей (--since/--year): тогда он годится
    # только если запрошенный период не уходит глубже этой границы.
    cached_since = payload.get("since")
    if cached_since:
        if since is None:
            LOG.info(
                "кеш %s собран только с %s — нужна полная история, собираю заново",
                path,
                cached_since,
            )
            return None
        if str(since) < str(cached_since):
            LOG.info(
                "кеш %s начинается с %s, а нужен период с %s — собираю заново",
                path,
                cached_since,
                since,
            )
            return None

    rows = payload.get("rows")
    if not isinstance(rows, list):
        LOG.warning("кеш %s без списка записей — собираю данные заново", path)
        return None

    if max_age_hours > 0:
        updated = payload.get("updated")
        moment = None
        if isinstance(updated, str):
            try:
                moment = datetime.fromisoformat(updated)
            except ValueError:
                moment = None
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - moment).total_seconds() / 3600
            if age_hours > max_age_hours:
                LOG.info(
                    "кеш %s устарел (%.1f ч > %.1f ч) — собираю данные заново",
                    path,
                    age_hours,
                    max_age_hours,
                )
                return None

    payload["rows"] = [row for row in rows if isinstance(row, dict)]
    return payload


def _write_json_atomic(path: str, payload: Mapping[str, Any]) -> bool:
    """Пишет JSON атомарно (через ``.tmp``); False — если запись не удалась."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temp = f"{path}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temp, path)
    except OSError as exc:
        LOG.warning("не удалось сохранить %s: %s", path, exc)
        return False
    return True


def save_cache(
    cache_dir: str,
    player_id: str,
    name: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    only_cotd: bool = False,
    since: date | None = None,
    zone: str = "",
    flag: str = "",
    flag_data: str = "",
) -> str | None:
    """Сохраняет собранные строки в кеш (атомарно, через .tmp)."""
    path = cache_path(cache_dir, player_id)
    payload = {
        "version": CACHE_VERSION,
        "player_id": str(player_id),
        "name": name or "",
        "zone": zone or "",
        "flag": flag or "",
        "flag_data": flag_data or "",
        "only_cotd": bool(only_cotd),
        "since": since.isoformat() if since else None,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": [dict(row) for row in rows],
    }
    uids = {
        str(row.get("map_date")): str(row["map_uid"])
        for row in rows
        if row.get("map_uid") and row.get("map_date")
    }
    if uids:
        payload["map_uids"] = uids
    if not _write_json_atomic(path, payload):
        return None
    LOG.info("кеш сохранён: %s (%d записей)", path, len(payload["rows"]))
    return path


def iter_caches(cache_dir: str) -> list[dict[str, Any]]:
    """Все пригодные кеши из папки — для сравнения игроков."""
    if not os.path.isdir(cache_dir):
        return []
    result: list[dict[str, Any]] = []
    for filename in sorted(os.listdir(cache_dir)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(cache_dir, filename)
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            LOG.debug("кеш %s пропущен: %s", path, exc)
            continue
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            continue
        rows = payload.get("rows")
        if not isinstance(rows, list):
            continue
        payload["rows"] = [row for row in rows if isinstance(row, dict)]
        result.append(payload)
    return result


def refresh_flags(args: argparse.Namespace) -> int:
    """Дозаполняет картинки флагов в кешах.

    Кеши, собранные до появления цепочки зон, остались без файла у тех зон, для
    которых trackmania.io картинку не отдаёт (город Мюнхен и подобные). Здесь
    добираем флаг ближайшей родительской зоны и пишем data URI назад в кеш,
    чтобы дальнейшая генерация сайта снова работала офлайн.
    """
    caches = iter_caches(args.cache_dir)
    if not caches:
        LOG.error("в %s нет кешей — нечего обновлять", args.cache_dir)
        return 1

    session = build_session()
    updated = 0
    missing = 0
    try:
        for payload in caches:
            if str(payload.get("flag_data") or "").startswith("data:image/"):
                continue
            player_id = str(payload.get("player_id") or "")
            if not player_id:
                continue
            profile = fetch_player_profile(
                session, player_id, base_url=args.base_url, timeout=args.timeout
            )
            chain = [str(payload.get("flag") or ""), *profile.flag_chain]
            flag_name, flag_data = fetch_zone_flag(
                session, chain, base_url=args.base_url, timeout=args.timeout
            )
            label = str(payload.get("name") or player_id)
            if not flag_data:
                missing += 1
                LOG.warning("флаг не нашёлся: %s", label)
                continue
            payload["flag"] = flag_name
            payload["flag_data"] = flag_data
            if profile.zone:
                payload["zone"] = profile.zone
            if profile.name and not payload.get("name"):
                payload["name"] = profile.name
            if _write_json_atomic(cache_path(args.cache_dir, player_id), payload):
                updated += 1
                LOG.info("флаг обновлён: %s (%s)", label, flag_name)
    finally:
        session.close()
    LOG.info("флаги: обновлено %d, без картинки %d", updated, missing)
    return 0


def filter_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    since: date | None = None,
    until: date | None = None,
    skip_unqualified: bool = False,
    stats: CollectStats | None = None,
) -> list[dict[str, Any]]:
    """Фильтрует уже собранные строки по периоду (кеш хранит полную историю)."""
    counters = stats if stats is not None else CollectStats()
    result: list[dict[str, Any]] = []

    for row in rows:
        map_date = parse_date(row.get("map_date"))
        if map_date is None:
            counters.no_date += 1
            continue
        if since is not None and map_date < since:
            counters.before_since += 1
            continue
        if until is not None and map_date > until:
            counters.after_until += 1
            continue
        if skip_unqualified and not row.get("div"):
            counters.unqualified += 1
            continue
        result.append(dict(row))

    return result


def build_partners_html(
    args: argparse.Namespace,
    player_id: str,
    current_rows: Sequence[Mapping[str, Any]],
) -> str:
    """Таблица «с кем чаще всего играл»: совпадение дня и дивизиона.

    Играли вместе = попали в один дивизион в один день (дивизионы COTD
    стартуют в разное время). Данные берутся из кешей остальных игроков.
    """
    others: list[report.PlayerRef] = []

    for payload in iter_caches(args.cache_dir):
        cached_id = str(payload.get("player_id") or "")
        if cached_id == str(player_id):
            continue
        rows = filter_rows(
            payload.get("rows", []),
            since=args.since,
            until=args.until,
            skip_unqualified=args.skip_unqualified,
        )
        if not rows:
            continue
        others.append(
            report.PlayerRef(
                name=str(payload.get("name") or cached_id or "?"),
                rows=rows,
                flag=str(payload.get("flag_data") or ""),
                zone=str(payload.get("zone") or ""),
            )
        )

    if not others:
        LOG.info("сравнивать не с кем: в кеше %s пока один игрок", args.cache_dir)
        return ""

    partners = report.compute_partners(current_rows, others)
    if not partners:
        LOG.info("общих дивизионов с игроками из кеша не нашлось")
        return ""
    return report.render_partners(partners)


def apply_year_progress(
    stats: report.Stats,
    all_rows: Sequence[Mapping[str, Any]],
    *,
    min_starts: int = report.PROGRESS_MIN_STARTS,
) -> None:
    """Дописывает в stats дельту к предыдущему году, если данных достаточно.

    Сравнивать год с годом имеет смысл, когда в обоих есть хотя бы несколько
    стартов; иначе дельта скачет от одного-двух результатов.
    """
    if stats.total < min_starts or not stats.year.isdigit():
        return
    prev_year = str(int(stats.year) - 1)
    prev_rows = [row for row in all_rows if str(row.get("map_date") or "")[:4] == prev_year]
    if len(prev_rows) < min_starts:
        return
    previous = report.compute_stats(
        prev_rows,
        name=stats.name,
        year=prev_year,
        flag=stats.flag,
        zone=stats.zone,
    )
    stats.progress = report.compute_year_progress(stats, previous)
    if stats.progress:
        stats.prev_year = prev_year


# -------------------------------------------------------------------------- CLI


def cli_date(text: str) -> date:
    """argparse-тип для дат YYYY-MM-DD с понятной ошибкой."""
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"ожидается дата в формате YYYY-MM-DD, получено {text!r}"
        ) from None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cotd-recap",
        description=(
            "Собирает таблицу кубков дня (COTD) игрока с trackmania.io "
            "в TSV/CSV. Колонки: " + ", ".join(COLUMNS) + "."
        ),
        epilog=(
            "ID игрока можно передать позиционно, через --player-id или "
            "через переменную окружения TMIO_PLAYER_ID. Базовый адрес API "
            "переопределяется --base-url / TMIO_API_BASE."
        ),
    )
    parser.add_argument(
        "player_id",
        nargs="?",
        help="tm.io id игрока (uuid или ник, как в URL trackmania.io)",
    )
    parser.add_argument(
        "--player-id",
        dest="player_id_opt",
        help="то же самое, если удобнее флагом",
    )
    parser.add_argument(
        "-s",
        "--since",
        type=cli_date,
        help="нижняя граница периода, YYYY-MM-DD (включительно); по умолчанию — вся история",
    )
    parser.add_argument(
        "-u",
        "--until",
        type=cli_date,
        help="верхняя граница периода, YYYY-MM-DD (включительно)",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("tsv", "csv"),
        default="tsv",
        help="разделитель вывода (по умолчанию tsv)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="файл для записи; по умолчанию stdout",
    )
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="не печатать строку заголовка",
    )
    parser.add_argument(
        "--skip-unqualified",
        action="store_true",
        help="не выводить дни, где нет дивизиона (div = 0)",
    )
    parser.add_argument(
        "--include-reruns",
        action="store_true",
        help="учитывать переигровки (includeReruns=true)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="пауза между запросами страниц, сек (по умолчанию 1.5, с джиттером)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"предохранитель от бесконечной пагинации (по умолчанию {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"таймаут запроса, сек (по умолчанию {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TMIO_API_BASE", API_BASE),
        help="базовый адрес API (по умолчанию %(default)s)",
    )
    parser.add_argument(
        "--dump-json",
        metavar="PATH",
        help="сохранить сырые ответы API (JSON Lines) — для разбора структуры",
    )
    parser.add_argument(
        "--html",
        metavar="PATH",
        help="собрать страницу со статистикой (например, docs/index.html для GitHub Pages)",
    )
    parser.add_argument(
        "--year",
        type=int,
        metavar="YYYY",
        help="ограничить период одним годом и подписать им страницу",
    )
    parser.add_argument(
        "--name",
        metavar="NICK",
        help="ник для заголовка страницы (по умолчанию берётся из профиля trackmania.io)",
    )
    parser.add_argument(
        "--only-cotd",
        action="store_true",
        help="оставить только записи, в названии которых есть 'cotd'",
    )
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("TMIO_CACHE_DIR", "cache"),
        metavar="PATH",
        help="папка с кешем собранных данных (по умолчанию %(default)s)",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=24.0,
        metavar="HOURS",
        help="сколько часов считать кеш свежим; 0 — не проверять возраст (по умолчанию %(default)s)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="не читать и не писать кеш",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="игнорировать кеш и пересобрать данные (кеш будет перезаписан)",
    )
    parser.add_argument(
        "--refresh-flags",
        action="store_true",
        help=(
            "дозаполнить в кеше картинки флагов (для зон без своего файла берётся "
            "флаг ближайшей родительской зоны); вместе с --site заодно пересобрать сайт"
        ),
    )
    parser.add_argument(
        "--partners",
        "--compare",
        dest="partners",
        action="store_true",
        help=(
            "добавить на --html страницу таблицу «с кем чаще всего играл» "
            "(совпадение дня и дивизиона с другими игроками из кеша)"
        ),
    )
    parser.add_argument(
        "--site",
        metavar="DIR",
        help=(
            "сгенерировать сайт из кеша: страница на каждого игрока "
            "+ index.html со сравнением и навигацией"
        ),
    )
    parser.add_argument(
        "--sort",
        default="avg_rank",
        metavar="KEY",
        choices=sorted(report.COMPARE_SORT_KEYS),
        help=(
            "по какой колонке сортировать таблицу сравнения (по умолчанию avg_rank); "
            "ключи: " + ", ".join(sorted(report.COMPARE_SORT_KEYS))
        ),
    )
    parser.add_argument(
        "--sort-desc",
        action="store_true",
        help="сортировать таблицу сравнения по убыванию",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="подробный лог (DEBUG) в stderr",
    )
    return parser.parse_args(argv)


def resolve_player_id(args: argparse.Namespace) -> str:
    """Позиционный аргумент > --player-id > TMIO_PLAYER_ID."""
    for candidate in (
        args.player_id,
        args.player_id_opt,
        os.environ.get("TMIO_PLAYER_ID"),
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return ""


def warn_unusual_player_id(player_id: str) -> None:
    """Мягкая подсказка: API стабильно работает с uuid, а не с ником."""
    if not _UUID_RE.match(player_id):
        LOG.info(
            "id %r не похож на uuid — если это ник, возьми uuid игрока из ссылки "
            "https://trackmania.io/#/player/<uuid>",
            player_id,
        )


def slugify(name: str, fallback: str) -> str:
    """Безопасное имя файла из ника: WalteN. -> walten."""
    slug = re.sub(r"[^0-9A-Za-z_-]+", "-", str(name)).strip("-").lower()
    return slug or fallback


@dataclass
class SitePlayer:
    """Игрок на сайте: кеш + посчитанная статистика."""

    player_id: str
    name: str
    slug: str
    stats: report.Stats | None
    rows: list[dict[str, Any]]
    flag: str = ""
    zone: str = ""
    map_uids: dict[str, str] = field(default_factory=dict)


def _write_flag_file(site_dir: str, flag_name: str, flag_data: str) -> str:
    """Кладёт флаг отдельным файлом в <site>/flags и отдаёт путь от года.

    При генерации сайта это выгоднее data URI: один и тот же флаг не дублируется
    в каждой ячейке каждой страницы. Если картинки в кеше нет, но файл остался
    с прошлой генерации — переиспользуем его (сайт собирается офлайн).
    """
    if not flag_name:
        return flag_data

    flags_dir = os.path.join(site_dir, "flags")
    if not flag_data.startswith("data:image/"):
        for ext in (".jpg", ".png"):
            if os.path.exists(os.path.join(flags_dir, f"{flag_name}{ext}")):
                return f"../flags/{quote(flag_name)}{ext}"
        return flag_data

    header, _, payload = flag_data.partition(",")
    try:
        raw = base64.b64decode(payload)
    except (ValueError, TypeError):
        return flag_data
    ext = ".png" if "png" in header else ".jpg"
    try:
        os.makedirs(flags_dir, exist_ok=True)
        path = os.path.join(flags_dir, f"{flag_name}{ext}")
        if not os.path.exists(path):
            with open(path, "wb") as handle:
                handle.write(raw)
    except OSError as exc:
        LOG.debug("не удалось сохранить флаг %s: %s", flag_name, exc)
        return flag_data
    return f"../flags/{quote(flag_name)}{ext}"


def _map_uids(payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Дата COTD -> uid карты (для ссылок на trackmania.io).

    Берём готовый словарь из кеша (его заполняет обогащение) и дополняем полем
    ``map_uid`` из строк: кеши, собранные новой версией, хранят uid в каждой строке.
    """
    result = {
        str(row.get("map_date")): str(row["map_uid"])
        for row in rows
        if row.get("map_uid") and row.get("map_date")
    }
    stored = payload.get("map_uids")
    if isinstance(stored, Mapping):
        result.update({str(key): str(value) for key, value in stored.items() if value})
    return result


def _years_of(rows: Sequence[Mapping[str, Any]]) -> set[int]:
    """Годы, которые встречаются в строках COTD."""
    years: set[int] = set()
    for row in rows:
        text = str(row.get("map_date") or "")[:4]
        if text.isdigit():
            years.add(int(text))
    return years


def build_site(args: argparse.Namespace, generated: str) -> int:
    """Сайт из кеша: docs/<год>/<игрок>.html с переключателем года."""
    caches = iter_caches(args.cache_dir)
    if not caches:
        LOG.error(
            "в %s нет кешей — сначала собери игроков обычным запуском (без --site)",
            args.cache_dir,
        )
        return 1

    prepared: list[SitePlayer] = []
    used_slugs: set[str] = set()

    for payload in caches:
        player_id = str(payload.get("player_id") or "")
        name = str(payload.get("name") or player_id or "player")
        rows = filter_rows(
            payload.get("rows", []),
            since=args.since,
            until=args.until,
            skip_unqualified=args.skip_unqualified,
        )
        if not rows:
            LOG.warning("пропускаю %s: под фильтр не попал ни один день", name)
            continue

        slug = slugify(name, f"player-{player_id[:8]}" if player_id else "player")
        if slug in used_slugs:
            slug = f"{slug}-{player_id[:8]}" if player_id else f"{slug}-2"
        used_slugs.add(slug)

        prepared.append(
            SitePlayer(
                player_id=player_id,
                name=name,
                slug=slug,
                stats=None,
                rows=rows,
                flag=_write_flag_file(
                    args.site,
                    str(payload.get("flag") or ""),
                    str(payload.get("flag_data") or ""),
                ),
                zone=str(payload.get("zone") or ""),
                map_uids=_map_uids(payload, rows),
            )
        )

    if not prepared:
        LOG.error("после фильтров не осталось ни одного игрока с данными")
        return 1

    years = sorted(
        year
        for year in {y for player in prepared for y in _years_of(player.rows)}
        if 2020 <= year <= 2026
    )
    if args.year is not None:
        if args.year not in years:
            LOG.error(
                "за %s нет данных: доступны %s",
                args.year,
                ", ".join(str(year) for year in years) or "ничего",
            )
            return 1
        years = [args.year]
    if not years:
        LOG.error("в кеше нет данных за 2020–2026")
        return 1

    os.makedirs(args.site, exist_ok=True)
    # Чистим прежнюю раскладку (docs/*.html и docs/<год>/), чтобы не оставался мусор.
    for entry in os.listdir(args.site):
        full = os.path.join(args.site, entry)
        if os.path.isdir(full) and (re.fullmatch(r"20\d\d", entry) or entry == report.ALL_TIME_KEY):
            shutil.rmtree(full, ignore_errors=True)
        elif os.path.isfile(full) and entry.endswith(".html"):
            try:
                os.remove(full)
            except OSError:
                pass

    written = 0
    # Общие CSS и JS — одни на весь сайт: браузер кеширует их после первой
    # страницы, и в каждой странице не дублируется ~19 КБ стилей и скриптов.
    assets_dir = os.path.join(args.site, report.SITE_ASSETS_DIR)
    try:
        os.makedirs(assets_dir, exist_ok=True)
        with open(os.path.join(assets_dir, report.SITE_CSS_NAME), "w", encoding="utf-8") as handle:
            handle.write(report.site_css())
        with open(os.path.join(assets_dir, report.SITE_JS_NAME), "w", encoding="utf-8") as handle:
            handle.write(report.site_js())
    except OSError as exc:
        LOG.error("не удалось записать общие файлы сайта: %s", exc)
        return 1
    assets_prefix = f"../{report.SITE_ASSETS_DIR}/"
    full_rows = {player.player_id: player.rows for player in prepared}

    periods: list[str] = [report.ALL_TIME_KEY, *(str(year) for year in years)]

    for period in periods:
        is_all_time = period == report.ALL_TIME_KEY
        year_label = "" if is_all_time else period
        period_players = [
            SitePlayer(
                player_id=player.player_id,
                name=player.name,
                slug=player.slug,
                stats=report.compute_stats(
                    rows,
                    name=player.name,
                    year=year_label,
                    flag=player.flag,
                    zone=player.zone,
                    href=f"{player.slug}.html",
                    map_uids=player.map_uids,
                ),
                rows=rows,
                flag=player.flag,
                zone=player.zone,
            )
            for player in prepared
            if (
                rows := (
                    player.rows
                    if is_all_time
                    else [r for r in player.rows if str(r.get("map_date") or "")[:4] == year_label]
                )
            )
        ]
        if not is_all_time:
            for player in period_players:
                apply_year_progress(player.stats, full_rows[player.player_id])
        if not period_players:
            continue

        out_dir = os.path.join(args.site, period)
        os.makedirs(out_dir, exist_ok=True)
        home = ("Таблица игроков", "index.html")

        for player in period_players:
            others = [
                report.PlayerRef(
                    other.name,
                    other.rows,
                    other.flag,
                    other.zone,
                    f"{other.slug}.html",
                )
                for other in period_players
                if other.player_id != player.player_id
            ]
            partners_html = report.render_partners(report.compute_partners(player.rows, others))
            player_years = sorted(
                y for y in _years_of(full_rows[player.player_id]) if y in set(years)
            )
            picker = report.render_year_select(
                [report.ALL_TIME_KEY, *player_years], period, page=f"{player.slug}.html"
            )
            page = report.render_html(
                player.stats,
                generated=generated,
                partners_html=partners_html,
                home=(home[0], home[1], False),
                year_picker=picker,
                assets=assets_prefix,
            )
            path = os.path.join(out_dir, f"{player.slug}.html")
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(page)
            except OSError as exc:
                LOG.error("не удалось записать %s: %s", path, exc)
                return 1
            written += 1
            LOG.info("%s (%s) -> %s", player.name, period, path)

        picker = report.render_year_select([report.ALL_TIME_KEY, *years], period, page="index.html")
        index_page = report.render_compare_page(
            [(player.name, player.stats, False) for player in period_players],
            year_picker=picker,
            generated=generated,
            year_label=year_label,
            sort_key=args.sort,
            descending=args.sort_desc,
            assets=assets_prefix,
        )
        index_path = os.path.join(out_dir, "index.html")
        try:
            with open(index_path, "w", encoding="utf-8") as handle:
                handle.write(index_page)
        except OSError as exc:
            LOG.error("не удалось записать %s: %s", index_path, exc)
            return 1

    root_index = os.path.join(args.site, "index.html")
    try:
        with open(root_index, "w", encoding="utf-8") as handle:
            handle.write(report.render_redirect(f"{years[-1]}/index.html", years))
    except OSError as exc:
        LOG.error("не удалось записать %s: %s", root_index, exc)
        return 1

    LOG.info(
        "сайт готов: %d страниц за годы %s + %s",
        written,
        ", ".join(str(year) for year in years),
        root_index,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    if args.refresh_flags:
        code = refresh_flags(args)
        if code != 0 or not args.site:
            return code

    if args.site:
        if args.since and args.until and args.since > args.until:
            LOG.error("--since (%s) позже --until (%s)", args.since, args.until)
            return 2
        return build_site(args, datetime.now().strftime("%d.%m.%Y %H:%M"))

    player_id = resolve_player_id(args)
    if not player_id:
        LOG.error(
            "не задан id игрока: передай его позиционно, через --player-id "
            "или в переменной окружения TMIO_PLAYER_ID"
        )
        return 2

    if args.since and args.until and args.since > args.until:
        LOG.error("--since (%s) позже --until (%s)", args.since, args.until)
        return 2
    if args.year is not None and not 2000 <= args.year <= 2100:
        LOG.error("--year: ожидается четырёхзначный год, получено %s", args.year)
        return 2
    if args.max_pages < 1:
        LOG.error("--max-pages должен быть >= 1")
        return 2

    warn_unusual_player_id(player_id)

    # --year задаёт границы периода, если они не заданы явно.
    if args.year is not None:
        if args.since is None:
            args.since = date(args.year, 1, 1)
        if args.until is None:
            args.until = date(args.year, 12, 31)

    session = build_session()
    stats = CollectStats()
    dump_handle: TextIO | None = None
    player_name = (args.name or "").strip()
    zone_name = ""
    flag_name = ""
    flag_data = ""
    flag_chain: tuple[str, ...] = ()
    map_uids: dict[str, str] = {}

    cached = None
    if not args.no_cache and not args.refresh:
        cached = load_cache(
            args.cache_dir,
            player_id,
            max_age_hours=args.cache_ttl,
            only_cotd=args.only_cotd,
            since=args.since,
        )

    if cached is not None:
        cached_rows = cached["rows"]
        stats.from_cache = True
        stats.fetched = len(cached_rows)
        player_name = player_name or str(cached.get("name") or "")
        zone_name = str(cached.get("zone") or "")
        flag_name = str(cached.get("flag") or "")
        flag_data = str(cached.get("flag_data") or "")
        map_uids = _map_uids(cached, cached_rows)
        LOG.info(
            "использую кеш: %d записей, обновлён %s (--refresh, чтобы пересобрать)",
            len(cached_rows),
            cached.get("updated", "?"),
        )
        session.close()
        raw_rows: list[dict[str, Any]] = cached_rows
    else:
        with contextlib.ExitStack() as stack:
            if args.dump_json:
                try:
                    dump_handle = stack.enter_context(open(args.dump_json, "w", encoding="utf-8"))
                except OSError as exc:
                    LOG.error("не удалось открыть %s на запись: %s", args.dump_json, exc)
                    session.close()
                    return 1

            try:
                if (args.html or not args.no_cache) and not player_name:
                    profile = fetch_player_profile(
                        session, player_id, base_url=args.base_url, timeout=args.timeout
                    )
                    player_name = profile.name
                    zone_name = profile.zone
                    flag_name = profile.flag
                    flag_chain = profile.flag_chain
                # Кеш хранит историю с нижней границы периода (если он задан):
                # так --year/--since экономят запросы к API.
                raw_rows = collect_rows(
                    session,
                    player_id,
                    since=args.since,
                    skip_unqualified=False,
                    cup_filter="cotd" if args.only_cotd else None,
                    stats=stats,
                    base_url=args.base_url,
                    include_reruns=args.include_reruns,
                    delay=args.delay,
                    max_pages=args.max_pages,
                    timeout=args.timeout,
                    dump=dump_handle,
                )
                if flag_name:
                    # У зоны может не быть своего файла флага (город Мюнхен) —
                    # тогда берём флаг ближайшей родительской зоны.
                    resolved, flag_data = fetch_zone_flag(
                        session,
                        [flag_name, *flag_chain],
                        base_url=args.base_url,
                        timeout=args.timeout,
                    )
                    if flag_data:
                        flag_name = resolved
                        LOG.info("флаг зоны: %s (%s)", zone_name or "?", flag_name)
            except PlayerNotFound as exc:
                LOG.error("%s", exc)
                return 1
            except RateLimited as exc:
                LOG.error("%s", exc)
                return 1
            except ApiError as exc:
                LOG.error("%s", exc)
                return 1
            except KeyboardInterrupt:
                LOG.error("прервано пользователем")
                return 130
            finally:
                session.close()

        if args.dump_json:
            LOG.info("сырые ответы API сохранены в %s", args.dump_json)

        if not args.no_cache:
            save_cache(
                args.cache_dir,
                player_id,
                player_name,
                raw_rows,
                only_cotd=args.only_cotd,
                since=args.since,
                zone=zone_name,
                flag=flag_name,
                flag_data=flag_data,
            )

    rows = filter_rows(
        raw_rows,
        since=args.since,
        until=args.until,
        skip_unqualified=args.skip_unqualified,
        stats=stats,
    )

    if args.html:
        page_year = str(args.year) if args.year is not None else guess_year(rows)
        page_stats = report.compute_stats(
            rows,
            name=player_name or player_id,
            year=page_year,
            flag=flag_data,
            zone=zone_name,
            map_uids=map_uids or _map_uids({}, rows),
        )
        apply_year_progress(page_stats, raw_rows)
        partners_html = ""
        if args.partners:
            partners_html = build_partners_html(args, player_id, rows)
        page = report.render_html(
            page_stats,
            generated=datetime.now().strftime("%d.%m.%Y %H:%M"),
            partners_html=partners_html,
        )
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.html)), exist_ok=True)
            with open(args.html, "w", encoding="utf-8") as handle:
                handle.write(page)
        except OSError as exc:
            LOG.error("не удалось записать %s: %s", args.html, exc)
            return 1
        LOG.info(
            "страница: %d COTD за %s, средний дивизион %s -> %s",
            page_stats.total,
            page_stats.year,
            f"{page_stats.avg_div:.1f}" if page_stats.avg_div is not None else "—",
            args.html,
        )
    elif args.partners:
        LOG.warning("--partners работает только вместе с --html — пропускаю")

    if not rows:
        LOG.warning("ничего не собрано — %s", stats.summary())
        if stats.fetched == 0:
            LOG.warning(
                "API не отдал ни одной записи: скорее всего неверный id игрока "
                "(нужен uuid из https://trackmania.io/#/player/<uuid>) "
                "или у игрока нет участий в COTD"
            )
        elif stats.no_date == stats.fetched:
            LOG.warning(
                "у всех записей не распознан timestamp — пришли дамп "
                "(--dump-json /tmp/raw.jsonl), поправлю разбор даты"
            )
        elif stats.fetched == stats.rejected():
            LOG.warning(
                "все полученные записи отсеяны фильтрами — ослабь --since/--until "
                "или убери --skip-unqualified"
            )

    delimiter = "\t" if args.format == "tsv" else ","

    if args.html and not args.output:
        # Страница собрана — таблицу в stdout не дублируем.
        return 0

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8", newline="") as handle:
                write_table(
                    rows,
                    handle,
                    delimiter=delimiter,
                    header=not args.no_header,
                )
        except OSError as exc:
            LOG.error("не удалось записать %s: %s", args.output, exc)
            return 1
        LOG.info("записано строк: %d -> %s (%s)", len(rows), args.output, stats.summary())
    else:
        try:
            write_table(
                rows,
                sys.stdout,
                delimiter=delimiter,
                header=not args.no_header,
            )
        except BrokenPipeError:  # например, при `| head`
            try:
                sys.stdout.close()
            except (BrokenPipeError, ValueError):
                pass
            return 0
        LOG.info("выведено строк: %d (%s)", len(rows), stats.summary())

    return 0


if __name__ == "__main__":
    sys.exit(main())
