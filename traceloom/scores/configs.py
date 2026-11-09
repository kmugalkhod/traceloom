"""Score configs: CRUD, archive, type immutability once used, and score validation (FR-S1, S2).

Every function takes the project id and filters on it. `ConfigCache` serves the ingest path with
immutable snapshots, so validating a batch of scores does not query once per event.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from traceloom.db.base import new_id, utcnow
from traceloom.db.models import ScoreConfig

DATA_TYPES = ("NUMERIC", "CATEGORICAL", "BOOLEAN")
EDITABLE_FIELDS = frozenset(
    {
        "name",
        "data_type",
        "min_value",
        "max_value",
        "categories",
        "description",
        "public_feedback",
    }
)
# Changing these after scores exist would make the stored scores invalid.
LOCKED_WHEN_USED = ("data_type", "categories")
NAME_MAX = 200

# Callback answering "does any score reference this config?" (the TraceStore owns the scores).
InUse = Callable[[str, str], bool]


class ConfigError(ValueError):
    """A config that cannot be saved; the message is safe to show to the user."""


class ConfigNotFound(LookupError):
    pass


# --- Validation of a score against a config ---------------------------------------------------


@dataclass(frozen=True)
class ConfigSnapshot:
    id: str
    name: str
    data_type: str
    min_value: float | None
    max_value: float | None
    labels: tuple[str, ...]
    archived: bool

    @classmethod
    def of(cls, config: ScoreConfig) -> ConfigSnapshot:
        return cls(
            config.id,
            config.name,
            config.data_type,
            config.min_value,
            config.max_value,
            tuple(c["label"] for c in config.categories or []),
            config.archived_at is not None,
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def check_score(config: ConfigSnapshot, value: Any, data_type: str | None = None) -> str | None:
    """Why `value` does not fit `config`, or None when it does. A BOOLEAN score may arrive as a
    JSON bool or as 0/1."""
    if data_type is not None and data_type != config.data_type:
        return f"data_type {data_type} does not match config {config.name!r} ({config.data_type})"
    if config.data_type == "NUMERIC":
        if not _is_number(value):
            return f"value must be a number for config {config.name!r}"
        if config.min_value is not None and value < config.min_value:
            return f"value {value} is below the minimum {config.min_value:g} of {config.name!r}"
        if config.max_value is not None and value > config.max_value:
            return f"value {value} is above the maximum {config.max_value:g} of {config.name!r}"
    elif config.data_type == "BOOLEAN":
        if not (isinstance(value, bool) or (_is_number(value) and value in (0, 1))):
            return f"value must be true/false (or 0/1) for config {config.name!r}"
    elif not isinstance(value, str) or value not in config.labels:
        return f"value must be one of {', '.join(config.labels)} for config {config.name!r}"
    return None


class ConfigCache:
    """Short-lived, per-project snapshots of configs for ingest validation. Config edits call
    `invalidate`; other processes see them within `ttl_seconds`. Type and categories cannot
    change once scores exist, so a stale snapshot never accepts a score that is invalid."""

    def __init__(self, session_factory: sessionmaker[Session], ttl_seconds: float = 10) -> None:
        self._sessions = session_factory
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], tuple[float, ConfigSnapshot | None]] = {}

    def get(self, project_id: str, config_id: str) -> ConfigSnapshot | None:
        key = (project_id, config_id)
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        with self._sessions() as db:
            config = db.scalar(
                select(ScoreConfig).where(
                    ScoreConfig.project_id == project_id, ScoreConfig.id == config_id
                )
            )
            snapshot = ConfigSnapshot.of(config) if config else None
        with self._lock:
            self._entries[key] = (now + self._ttl, snapshot)
        return snapshot

    def invalidate(self, project_id: str, config_id: str | None = None) -> None:
        with self._lock:
            for key in [k for k in self._entries if k[0] == project_id]:
                if config_id is None or key[1] == config_id:
                    del self._entries[key]

    def check(
        self, project_id: str, config_id: str, value: Any, data_type: str | None = None
    ) -> str | None:
        """Error message for a score naming `config_id`, or None when it is valid."""
        snapshot = self.get(project_id, config_id)
        if snapshot is None:
            return f"unknown config_id {config_id!r}"
        return check_score(snapshot, value, data_type)


def score_check_for(cache: ConfigCache) -> Callable[[str, Any], str | None]:
    """The `accept_batch` hook: checks a ScoreBody that names a config_id."""

    def check(project_id: str, body: Any) -> str | None:
        if not body.config_id:
            return None  # no config: accepted as before
        return cache.check(project_id, body.config_id, body.value, body.data_type)

    return check


def config_cache(container: Any) -> ConfigCache:
    """The container's cache, built on first use."""
    if container.score_config_cache is None:
        container.score_config_cache = ConfigCache(
            container.session_factory, container.settings.eval_rule_cache_seconds
        )
    return container.score_config_cache


# --- CRUD -------------------------------------------------------------------------------------


def _normalize_categories(raw: Iterable[Any] | None) -> list[dict[str, Any]]:
    categories: list[dict[str, Any]] = []
    for index, item in enumerate(raw or []):
        label, number = (
            (item.get("label"), item.get("value", index))
            if isinstance(item, Mapping)
            else (item, index)
        )
        label = label.strip() if isinstance(label, str) else ""
        if not label or len(label) > NAME_MAX:
            raise ConfigError("Every category needs a label of up to 200 characters.")
        if not _is_number(number):
            raise ConfigError(f"Category {label!r} needs a numeric value.")
        categories.append({"label": label, "value": number})
    if len({c["label"] for c in categories}) != len(categories):
        raise ConfigError("Category labels must be unique.")
    return categories


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise a full set of config fields."""
    out = dict(fields)
    name = (out.get("name") or "").strip()
    if not name or len(name) > NAME_MAX:
        raise ConfigError("Give the config a name of up to 200 characters.")
    out["name"] = name
    data_type = out.get("data_type")
    if data_type not in DATA_TYPES:
        raise ConfigError(f"data_type must be one of {', '.join(DATA_TYPES)}.")
    if data_type == "NUMERIC":
        lo, hi = out.get("min_value"), out.get("max_value")
        if any(v is not None and not _is_number(v) for v in (lo, hi)):
            raise ConfigError("Range limits must be numbers.")
        if lo is not None and hi is not None and lo > hi:
            raise ConfigError("The minimum must not be above the maximum.")
        out["categories"] = None
    else:
        out["min_value"] = out["max_value"] = None
        if data_type == "CATEGORICAL":
            out["categories"] = _normalize_categories(out.get("categories"))
            if not out["categories"]:
                raise ConfigError("A categorical config needs at least one category.")
        else:
            out["categories"] = None
    out["description"] = (out.get("description") or "").strip()
    out["public_feedback"] = bool(out.get("public_feedback"))
    return out


def list_configs(db: Session, project_id: str, include_archived: bool = False) -> list[ScoreConfig]:
    query = select(ScoreConfig).where(ScoreConfig.project_id == project_id)
    if not include_archived:
        query = query.where(ScoreConfig.archived_at.is_(None))
    return list(db.scalars(query.order_by(ScoreConfig.name, ScoreConfig.created_at)))


def get_config(db: Session, project_id: str, config_id: str) -> ScoreConfig:
    config = db.scalar(
        select(ScoreConfig).where(ScoreConfig.project_id == project_id, ScoreConfig.id == config_id)
    )
    if config is None:
        raise ConfigNotFound(config_id)
    return config


def _live_name_taken(db: Session, project_id: str, name: str, except_id: str | None) -> bool:
    query = select(ScoreConfig.id).where(
        ScoreConfig.project_id == project_id,
        ScoreConfig.name == name,
        ScoreConfig.archived_at.is_(None),
    )
    if except_id:
        query = query.where(ScoreConfig.id != except_id)
    return db.scalar(query.limit(1)) is not None


def _save(db: Session, project_id: str, config: ScoreConfig, cache: ConfigCache | None) -> None:
    with db.no_autoflush:  # a pending duplicate must reach the check, not the database
        taken = _live_name_taken(db, project_id, config.name, config.id)
    if taken:
        raise ConfigError(f"A score config named {config.name!r} already exists.")
    try:
        db.commit()  # the partial unique index also catches a concurrent duplicate
    except IntegrityError:
        db.rollback()
        raise ConfigError(f"A score config named {config.name!r} already exists.") from None
    if cache:
        cache.invalidate(project_id, config.id)


def create_config(
    db: Session,
    project_id: str,
    *,
    created_by: str | None = None,
    cache: ConfigCache | None = None,
    **fields: Any,
) -> ScoreConfig:
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ConfigError(f"Unknown field: {sorted(unknown)[0]}.")
    values = _clean(fields)
    config = ScoreConfig(id=new_id(), project_id=project_id, created_by=created_by, **values)
    db.add(config)
    try:
        _save(db, project_id, config, cache)
    except ConfigError:
        db.expunge(config)
        raise
    return config


def update_config(
    db: Session,
    project_id: str,
    config_id: str,
    changes: Mapping[str, Any],
    *,
    in_use: InUse,
    cache: ConfigCache | None = None,
) -> ScoreConfig:
    """Apply `changes`. Description, range, name and public_feedback can always change; the data
    type and categories only while no score references the config."""
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise ConfigError(f"Unknown field: {sorted(unknown)[0]}.")
    config = get_config(db, project_id, config_id)
    current = {name: getattr(config, name) for name in EDITABLE_FIELDS}
    merged = _clean({**current, **changes})
    locked = [n for n in LOCKED_WHEN_USED if n in changes and merged[n] != current[n]]
    if locked and in_use(project_id, config_id):
        raise ConfigError(
            f"The {' and '.join(n.replace('_', ' ') for n in locked)} cannot change once "
            "scores use this config. Archive it and create a new one."
        )
    for name, value in merged.items():
        setattr(config, name, value)
    config.updated_at = utcnow()
    try:
        _save(db, project_id, config, cache)
    except ConfigError:
        db.rollback()
        raise
    return config


def set_archived(
    db: Session,
    project_id: str,
    config_id: str,
    archived: bool,
    *,
    cache: ConfigCache | None = None,
) -> ScoreConfig:
    """Archive hides the config from new-annotation pickers; its scores stay. Unarchiving fails
    when another live config has taken the name."""
    config = get_config(db, project_id, config_id)
    config.archived_at = utcnow() if archived else None
    config.updated_at = utcnow()
    try:
        _save(db, project_id, config, cache)
    except ConfigError:
        db.rollback()
        raise ConfigError(
            f"Another config named {config.name!r} is active; rename one of them first."
        ) from None
    return config
