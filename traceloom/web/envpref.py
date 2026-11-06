"""The environment a data page shows: `?env=` wins, `env=all` means every environment, otherwise
the user's saved choice (user_prefs key `env`, the same one the traces list reads and its
switcher writes)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from traceloom.db.models import UserPref


def saved_env(db: Session, user_id: str, project_id: str) -> str | None:
    row = db.get(UserPref, (user_id, project_id, "env"))
    return row.value if row and row.value else None


def resolve_env(params: Mapping[str, str], pref: str | None) -> str | None:
    env = params.get("env")
    if env == "all":
        return None
    return env or pref or None


def switcher_context(store: Any, project_id: str, selected: str | None, pref: str | None) -> dict:
    """What `_env_switcher.html` needs. Empty (no switcher) until the store can list the
    project's environments."""
    list_environments = getattr(store, "list_environments", None)
    if list_environments is None:
        return {}
    options = list_environments(project_id)
    if selected and selected not in options:
        options = sorted({*options, selected})
    return {"env_ctx": {"options": options, "current": selected, "saved": pref}}
