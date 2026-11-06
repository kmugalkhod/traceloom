"""Model definitions: the seed file plus per-project overrides, matched by regex and date."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from traceloom.db.models import ModelDefinition

SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "models.yaml"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Tier:
    threshold: int  # applies when input tokens exceed this
    prices: dict[str, float]


@dataclass(frozen=True)
class ModelDef:
    id: str
    project_id: str | None
    model_name: str
    pattern: re.Pattern[str]
    tokenizer: str | None
    prices: dict[str, float]
    tiers: tuple[Tier, ...] = field(default_factory=tuple)
    valid_from: datetime = EPOCH
    source: str = "user"


def to_model_def(row: ModelDefinition) -> ModelDef:
    return ModelDef(
        id=row.id,
        project_id=row.project_id,
        model_name=row.model_name,
        pattern=re.compile(row.match_pattern),
        tokenizer=row.tokenizer,
        prices={k: float(v) for k, v in (row.prices or {}).items()},
        tiers=tuple(
            sorted(
                (
                    Tier(int(t["threshold"]), {k: float(v) for k, v in t["prices"].items()})
                    for t in (row.tiers or [])
                ),
                key=lambda t: t.threshold,
            )
        ),
        valid_from=row.valid_from or EPOCH,
        source=row.source,
    )


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


class ModelRegistry:
    """Loads definitions per scope (one project, or global) and caches them briefly."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessions = sessions
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str | None, tuple[float, list[ModelDef]]] = {}

    # --- seed -------------------------------------------------------------------------------

    def load_seed(self, path: Path = SEED_PATH) -> int:
        """Upsert the seed file as global definitions and drop seed rows it no longer lists."""
        entries = yaml.safe_load(path.read_text())["models"]
        with self._sessions.begin() as s:
            stored = {
                r.model_name: r
                for r in s.scalars(
                    select(ModelDefinition).where(
                        ModelDefinition.project_id.is_(None), ModelDefinition.source == "seed"
                    )
                )
            }
            for e in entries:
                re.compile(e["match"])  # fail startup on a broken seed, not at match time
                values = {
                    "match_pattern": e["match"],
                    "tokenizer": e.get("tokenizer"),
                    "prices": e["prices"],
                    "tiers": e.get("tiers") or [],
                    "valid_from": _as_datetime(e.get("valid_from")),
                }
                row = stored.pop(e["model_name"], None)
                if row is None:
                    s.add(
                        ModelDefinition(
                            project_id=None, model_name=e["model_name"], source="seed", **values
                        )
                    )
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
            for row in stored.values():
                s.execute(delete(ModelDefinition).where(ModelDefinition.id == row.id))
        self.invalidate()
        return len(entries)

    # --- lookup -----------------------------------------------------------------------------

    def invalidate(self, project_id: str | None = None) -> None:
        """Drop cached definitions: one project's scope, or everything when no id is given."""
        with self._lock:
            if project_id is None:
                self._cache.clear()
            else:
                self._cache.pop(project_id, None)

    def _scope(self, project_id: str | None) -> list[ModelDef]:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(project_id)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        with self._sessions() as s:
            where = (
                ModelDefinition.project_id.is_(None)
                if project_id is None
                else ModelDefinition.project_id == project_id
            )
            defs = [to_model_def(r) for r in s.scalars(select(ModelDefinition).where(where))]
        with self._lock:
            self._cache[project_id] = (now, defs)
        return defs

    def find(self, project_id: str | None, model: str | None, at: datetime) -> ModelDef | None:
        """Project definitions beat global ones; within a scope the latest valid_from <= `at`
        wins."""
        if not model:
            return None
        scopes = ([project_id] if project_id else []) + [None]
        for scope in scopes:
            matches = [
                d for d in self._scope(scope) if d.valid_from <= at and d.pattern.search(model)
            ]
            if matches:
                return max(matches, key=lambda d: (d.valid_from, d.id))
        return None
