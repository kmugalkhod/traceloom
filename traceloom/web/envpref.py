"""The environment a data page shows: `?env=` wins, `env=all` means every environment, otherwise
the user's saved choice (the `env` preference the traces list switcher writes)."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.orm import Session

from traceloom.services import views


def saved_env(db: Session, user_id: str, project_id: str) -> str | None:
    return views.get_pref(db, user_id, project_id, "env") or None


def resolve_env(params: Mapping[str, str], pref: str | None) -> str | None:
    env = params.get("env")
    if env == "all":
        return None
    return env or pref or None
