"""The traces list page: URL state <-> TraceQuery, filter chips and row view-models.

All filter, search, range and sort state lives in the URL query string. Only the column choice
and the environment are stored per user and project (user_prefs).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from starlette.datastructures import QueryParams

from traceloom.ports import LEVELS, TraceQuery, TraceRow
from traceloom.web.context import avatar_class, initials

PAGE_SIZE = 50
COLUMNS = (
    ("time", "Time"),
    ("name", "Name"),
    ("user", "User"),
    ("session", "Session"),
    ("tags", "Tags"),
    ("latency", "Latency"),
    ("tokens", "Tokens"),
    ("cost", "Cost"),
    ("level", "Level"),
    ("scores", "Scores"),
    ("environment", "Environment"),
    ("release", "Release"),
)
COLUMN_KEYS = tuple(k for k, _ in COLUMNS)
DEFAULT_COLUMNS = ("time", "name", "user", "tags", "latency", "cost", "level", "scores")
RANGES = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_RANGE = "24h"
SORTS = {"time": "timestamp", "latency": "latency", "cost": "cost", "tokens": "tokens"}
TABS = ("all", "errors", "warnings", "bookmarked")
MAX_METADATA_FILTERS = 10


def parse_columns(stored: str | None) -> tuple[str, ...]:
    """The user's column choice, in canonical order; anything unknown or empty -> default."""
    if not stored:
        return DEFAULT_COLUMNS
    chosen = {c for c in stored.split(",") if c in COLUMN_KEYS}
    ordered = tuple(k for k in COLUMN_KEYS if k in chosen)
    return ordered or DEFAULT_COLUMNS


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _day(value: str | None, end: bool = False) -> datetime | None:
    try:
        parsed = datetime.strptime(value or "", "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed + timedelta(days=1) - timedelta(milliseconds=1) if end else parsed


@dataclass
class Chip:
    label: str
    remove: dict[str, str | None]  # param -> None drops it; used with ListState.url(set=...)
    value: str | None = None  # drop only this value of a multi-valued param


@dataclass
class ListState:
    query: TraceQuery
    params: QueryParams
    range: str
    tab: str
    sort: str  # "time.desc"
    search: str
    form: dict[str, object]
    environment: str | None
    chips: list[Chip] = field(default_factory=list)
    base_path: str = ""

    def url(
        self,
        drop: Sequence[str] = (),
        set: Mapping[str, str] | None = None,
        only: Sequence[str] | None = None,
        remove_value: tuple[str, str] | None = None,
    ) -> str:
        """This page's URL with some params dropped or replaced. Paging is always reset."""
        pairs: list[tuple[str, str]] = []
        for key, value in self.params.multi_items():
            if key in ("cursor", "shown") or key in drop or (set and key in set):
                continue
            if remove_value and (key, value) == remove_value:
                continue
            pairs.append((key, value))
        if set:
            pairs.extend((k, v) for k, v in set.items() if v is not None)
        return f"{self.base_path}?{urlencode(pairs)}" if pairs else self.base_path

    @property
    def range_phrase(self) -> str:
        labels = {
            "1h": "in the last hour",
            "24h": "in the last 24 hours",
            "7d": "in the last 7 days",
            "30d": "in the last 30 days",
        }
        return labels.get(self.range, "in the selected dates")

    @property
    def range_label(self) -> str:
        return (
            self.range_phrase.replace("in the", "In the").replace("in the", "In the")
            if self.range != "custom"
            else "In the selected dates"
        )

    def with_cursor(self, cursor: str, shown: int = 0) -> str:
        pairs = [(k, v) for k, v in self.params.multi_items() if k not in ("cursor", "shown")]
        pairs += [("cursor", cursor), ("shown", str(shown))]
        return f"{self.base_path}?{urlencode(pairs)}"

    @property
    def filter_query(self) -> str:
        """The params that describe the filters (what a saved view stores)."""
        return urlencode(
            [(k, v) for k, v in self.params.multi_items() if k not in ("cursor", "shown")]
        )

    @property
    def is_filtered(self) -> bool:
        keys = {k for k, _ in self.params.multi_items()} - {"cursor", "shown", "sort", "range"}
        return bool(keys) or self.range != DEFAULT_RANGE

    def sort_url(self, column: str) -> str:
        current, _, direction = self.sort.partition(".")
        nxt = "asc" if (current == column and direction == "desc") else "desc"
        return self.url(set={"sort": f"{column}.{nxt}"})

    def sort_state(self, column: str) -> str:
        current, _, direction = self.sort.partition(".")
        return (
            ("ascending" if direction == "asc" else "descending") if current == column else "none"
        )


def parse_state(
    params: QueryParams, base_path: str, env_pref: str | None, now: datetime | None = None
) -> ListState:
    now = now or datetime.now(UTC)
    get = params.get

    rng = get("range") or DEFAULT_RANGE
    if rng not in RANGES and rng != "custom":
        rng = DEFAULT_RANGE
    if rng == "custom":
        from_time, to_time = _day(get("from")), _day(get("to"), end=True)
        if from_time is None and to_time is None:
            rng, from_time = DEFAULT_RANGE, now - RANGES[DEFAULT_RANGE]
    else:
        from_time, to_time = now - RANGES[rng], None

    sort_key, _, direction = (get("sort") or "time.desc").partition(".")
    if sort_key not in SORTS:
        sort_key = "time"
    direction = direction if direction in ("asc", "desc") else "desc"

    tab = get("tab") if get("tab") in TABS else "all"
    level = get("level") if get("level") in LEVELS else None
    min_level = level
    has_error = {"1": True, "0": False}.get(get("has_error") or "")
    bookmarked = True if (get("bookmarked") == "1" or tab == "bookmarked") else None
    if tab == "errors":
        has_error = True
    if tab == "warnings" and (
        min_level is None or LEVELS.index(min_level) < LEVELS.index("WARNING")
    ):
        min_level = "WARNING"

    metadata = {}
    for key, value in params.multi_items():
        if (
            key.startswith("metadata.")
            and len(key) > len("metadata.")
            and len(metadata) < MAX_METADATA_FILTERS
        ):
            metadata[key[len("metadata.") :]] = value

    env_param = get("env")
    environment = None if env_param == "all" else (env_param or env_pref or None)

    lat_min, lat_max = _float(get("lat_min")), _float(get("lat_max"))
    query = TraceQuery(
        q=(get("q") or "").strip() or None,
        name=get("name") or None,
        user_id=get("user") or None,
        session_id=get("session") or None,
        tags=tuple(t for t in params.getlist("tag") if t),
        min_level=min_level,
        environment=environment,
        release=get("release") or None,
        version=get("version") or None,
        has_error=has_error,
        metadata=metadata,
        latency_min_ms=lat_min * 1000 if lat_min is not None else None,
        latency_max_ms=lat_max * 1000 if lat_max is not None else None,
        cost_min=_float(get("cost_min")),
        cost_max=_float(get("cost_max")),
        bookmarked=bookmarked,
        from_time=from_time,
        to_time=to_time,
        sort=SORTS[sort_key],
        descending=direction == "desc",
        limit=PAGE_SIZE,
    )
    state = ListState(
        query=query,
        params=params,
        range=rng,
        tab=tab,
        sort=f"{sort_key}.{direction}",
        search=get("q") or "",
        form={
            "name": get("name") or "",
            "user": get("user") or "",
            "session": get("session") or "",
            "tag": ", ".join(t for t in params.getlist("tag") if t),
            "level": level or "",
            "release": get("release") or "",
            "version": get("version") or "",
            "has_error": get("has_error") or "",
            "bookmarked": get("bookmarked") == "1",
            "lat_min": get("lat_min") or "",
            "lat_max": get("lat_max") or "",
            "cost_min": get("cost_min") or "",
            "cost_max": get("cost_max") or "",
            "from": get("from") or "",
            "to": get("to") or "",
            "metadata": metadata,
        },
        environment=environment,
        base_path=base_path,
    )
    state.chips = chips_for(state)
    return state


def chips_for(state: ListState) -> list[Chip]:
    p = state.params
    chips: list[Chip] = []
    simple = (
        ("name", "name"),
        ("user", "user"),
        ("session", "session"),
        ("release", "release"),
        ("version", "version"),
    )
    for key, label in simple:
        if p.get(key):
            chips.append(Chip(f"{label}: {p.get(key)}", {key: None}))
    for tag in p.getlist("tag"):
        if tag:
            chips.append(Chip(f"tag: {tag}", {"tag": None}, value=tag))
    if state.form["level"]:
        chips.append(Chip(f"level ≥ {state.form['level']}", {"level": None}))
    if p.get("has_error") in ("0", "1"):
        chips.append(
            Chip("has error" if p.get("has_error") == "1" else "no errors", {"has_error": None})
        )
    if p.get("bookmarked") == "1":
        chips.append(Chip("bookmarked", {"bookmarked": None}))
    for key, value in state.form["metadata"].items():
        chips.append(Chip(f"metadata.{key} = {value}", {f"metadata.{key}": None}))
    for low, high, label, unit in (
        ("lat_min", "lat_max", "latency", " s"),
        ("cost_min", "cost_max", "cost", ""),
    ):
        if p.get(low) or p.get(high):
            chips.append(
                Chip(
                    f"{label} {p.get(low) or '0'}–{p.get(high) or '∞'}{unit}",
                    {low: None, high: None},
                )
            )
    if state.range == "custom":
        chips.append(
            Chip(
                f"{p.get('from') or '…'} to {p.get('to') or 'now'}",
                {"range": None, "from": None, "to": None},
            )
        )
    return chips


def chip_url(state: ListState, chip: Chip) -> str:
    if chip.value is not None:
        key = next(iter(chip.remove))
        return state.url(remove_value=(key, chip.value))
    return state.url(drop=list(chip.remove))


@dataclass
class Row:
    id: str
    href: str
    day: str
    clock: str
    name: str
    short_id: str
    observation_count: int
    bookmarked: bool
    user: str | None
    user_initials: str
    user_av: str
    session: str | None
    tags: list[str]
    latency_ms: float | None
    tokens: int
    cost: float
    level: str
    environment: str
    release: str | None
    scores: list[tuple[str, str]]
    more_scores: int

    @property
    def row_class(self) -> str:
        return self.level if self.level in ("ERROR", "WARNING") else ""


def _score_value(score) -> str:
    if score.string_value:
        return score.string_value
    if score.data_type == "BOOLEAN":
        return "true" if score.value else "false"
    return "" if score.value is None else f"{score.value:g}"


def row_model(project_id: str, trace: TraceRow) -> Row:
    scores = [(s.name, _score_value(s)) for s in trace.scores]
    return Row(
        id=trace.id,
        href=f"/project/{project_id}/traces/{trace.id}",
        day=trace.timestamp.astimezone(UTC).strftime("%b %-d, %Y"),
        clock=trace.timestamp.astimezone(UTC).strftime("%H:%M:%S"),
        name=trace.name or "(unnamed)",
        short_id=trace.id if len(trace.id) <= 20 else trace.id[:20] + "…",
        observation_count=trace.observation_count,
        bookmarked=trace.bookmarked,
        user=trace.user_id,
        user_initials=initials(trace.user_id or ""),
        user_av=avatar_class(trace.user_id or ""),
        session=trace.session_id,
        tags=list(trace.tags),
        latency_ms=trace.latency_ms,
        tokens=trace.total_tokens,
        cost=trace.total_cost,
        level=trace.max_level,
        environment=trace.environment,
        release=trace.release,
        scores=scores[:3],
        more_scores=max(len(scores) - 3, 0),
    )


FILTER_PARAMS = (
    "q",
    "name",
    "user",
    "session",
    "tag",
    "level",
    "has_error",
    "release",
    "version",
    "bookmarked",
    "lat_min",
    "lat_max",
    "cost_min",
    "cost_max",
    "from",
    "to",
    "range",
    "sort",
    "tab",
    "env",
    "cursor",
    "shown",
)


def canonical_params(params: QueryParams) -> list[tuple[str, str]]:
    """The tidy form of a submitted filter form: blanks dropped, `metadata_key`/`metadata_value`
    turned into `metadata.<key>`, comma-separated tags split, and a date implies a custom range."""
    pairs: list[tuple[str, str]] = []
    meta_key = (params.get("metadata_key") or "").strip()
    meta_value = (params.get("metadata_value") or "").strip()
    for key, value in params.multi_items():
        value = value.strip()
        if key in ("metadata_key", "metadata_value") or not value:
            continue
        if key == "tag":
            pairs.extend(("tag", t.strip()) for t in value.split(",") if t.strip())
        elif key == "range" and value == "custom" and not (params.get("from") or params.get("to")):
            continue
        else:
            pairs.append((key, value))
    if meta_key and meta_value:
        pairs.append((f"metadata.{meta_key}", meta_value))
    if (params.get("from") or params.get("to")) and not any(k == "range" for k, _ in pairs):
        pairs.append(("range", "custom"))
    elif params.get("from") or params.get("to"):
        pairs = [(k, "custom" if k == "range" else v) for k, v in pairs]
    return pairs
