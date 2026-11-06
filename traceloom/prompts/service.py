"""Prompt service (FR-P1..P7): versions, labels, reads, listing, deletion, protected labels.

Every function takes the project and the acting `Actor`, scopes every query to that project and
re-checks the permission, so the rules hold for the UI and the public API alike. Writes add
audit rows and webhook events to the caller's transaction (the caller commits); a permission
or validation failure raises before anything is written.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from traceloom.auth.permissions import Perm, Role, can
from traceloom.db.base import utcnow
from traceloom.db.models import (
    PRODUCTION_LABEL,
    Project,
    Prompt,
    PromptLabel,
    PromptVersion,
    ProtectedLabel,
    User,
)
from traceloom.prompts.composition import (
    CompositionError,
    Reference,
    Resolved,
    Resolver,
    resolve_content,
)
from traceloom.services import audit
from traceloom.webhooks import events

NAME_RE = re.compile(r"[A-Za-z0-9_.\-/]{1,200}")
LABEL_RE = re.compile(r"[a-z0-9_\-]{1,36}")
LATEST = "latest"
TYPES = ("text", "chat")
ROLES = ("system", "user", "assistant", "placeholder")
MAX_TAGS = 20
MAX_TAG_LENGTH = 50
SORTS = ("name", "updated")


class PromptError(ValueError):
    """The request is invalid (the message is safe to show)."""


class PromptNotFound(LookupError):
    pass


class NotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class Actor:
    """Who is acting: a signed-in user (with their org role) or a project API key."""

    type: str  # user | api_key
    id: str | None
    role: Role | None = None
    name: str = ""
    may_move_protected: bool = False

    @classmethod
    def for_user(cls, user: User, role: Role | None) -> Actor:
        return cls("user", user.id, role, user.email)

    @classmethod
    def for_key(cls, key_id: str, *, may_move_protected: bool = False) -> Actor:
        return cls("api_key", key_id, None, f"api_key:{key_id}", may_move_protected)

    def can(self, perm: Perm) -> bool:
        if self.type == "api_key":
            if perm is Perm.PROMPT_LABELS_PROTECTED:
                return self.may_move_protected
            return perm in (Perm.PROMPTS_READ, Perm.PROMPTS_WRITE)
        return can(self.role, perm)

    def require(self, perm: Perm, message: str) -> None:
        if not self.can(perm):
            raise NotAllowed(message)

    @property
    def label(self) -> str:
        return self.name or self.id or self.type


@dataclass(frozen=True)
class PromptView:
    prompt_id: str
    name: str
    type: str
    version: int
    prompt: Any  # str (text) or list of messages (chat); resolved unless resolve=False
    config: dict[str, Any]
    labels: list[str]
    tags: list[str]
    commit_message: str
    dependencies: list[dict[str, Any]]
    created_by: str | None
    created_at: datetime
    latest_version: int


@dataclass(frozen=True)
class PromptSummary:
    name: str
    type: str
    tags: list[str]
    latest_version: int
    labels: dict[str, int]
    updated_at: datetime
    updated_by: str | None = None  # who saved the latest version (user or key id)
    snippet: str = ""  # the matching line when a search matched the content only

    @property
    def folder(self) -> str:
        return self.name.rpartition("/")[0]


@dataclass(frozen=True)
class PromptList:
    items: list[PromptSummary]
    total: int
    folders: list[tuple[str, int]] = field(default_factory=list)  # (child folder path, prompts)
    page: int = 1
    limit: int = 50


# --- validation -----------------------------------------------------------------------------


def validate_name(name: str) -> str:
    """FR-P1: `[A-Za-z0-9_.\\-/]{1,200}`; `/` builds folders."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise PromptError(
            "Prompt names use letters, digits and _ . - / only, and are 1 to 200 characters."
        )
    return name


def validate_label(label: str) -> str:
    if not isinstance(label, str) or not LABEL_RE.fullmatch(label):
        raise PromptError("Labels use lowercase letters, digits, _ and - only (1 to 36).")
    if label == LATEST:
        raise PromptError("`latest` is automatic and cannot be set by hand.")
    return label


def _tags(tags: Any) -> list[str]:
    if not isinstance(tags, (list, tuple)):
        raise PromptError("Tags must be a list of strings.")
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            raise PromptError("Tags must be non-empty strings.")
        tag = tag.strip()
        if len(tag) > MAX_TAG_LENGTH:
            raise PromptError(f"Tags must be {MAX_TAG_LENGTH} characters or fewer.")
        if tag not in out:
            out.append(tag)
    if len(out) > MAX_TAGS:
        raise PromptError(f"A prompt can have at most {MAX_TAGS} tags.")
    return out


def _content(prompt_type: str, content: Any) -> Any:
    if prompt_type == "text":
        if not isinstance(content, str):
            raise PromptError("A text prompt's content must be a string.")
        return content
    if not isinstance(content, list) or not content:
        raise PromptError("A chat prompt needs a list of at least one message.")
    messages = []
    for i, message in enumerate(content, 1):
        if not isinstance(message, dict) or message.get("role") not in ROLES:
            raise PromptError(f"Message {i} needs a role of {', '.join(ROLES)}.")
        if not isinstance(message.get("content"), str):
            raise PromptError(f"Message {i} needs string content.")
        messages.append({"role": message["role"], "content": message["content"]})
    return messages


def _config(config: Any) -> dict[str, Any]:
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise PromptError("Config must be a JSON object.")
    try:
        json.dumps(config)
    except (TypeError, ValueError) as exc:
        raise PromptError("Config must be valid JSON.") from exc
    return config


# --- lookups (all project-scoped) -----------------------------------------------------------


def _prompt(db: Session, project: Project, name: str) -> Prompt | None:
    return db.scalar(select(Prompt).where(Prompt.project_id == project.id, Prompt.name == name))


def _require_prompt(db: Session, project: Project, name: str) -> Prompt:
    prompt = _prompt(db, project, name)
    if prompt is None:
        raise PromptNotFound(f"Prompt {name!r} not found.")
    return prompt


def _latest_version(db: Session, project_id: str, prompt_id: str) -> int:
    return (
        db.scalar(
            select(func.max(PromptVersion.version)).where(
                PromptVersion.project_id == project_id, PromptVersion.prompt_id == prompt_id
            )
        )
        or 0
    )


def _version_row(
    db: Session, project_id: str, prompt_id: str, version: int
) -> PromptVersion | None:
    return db.scalar(
        select(PromptVersion).where(
            PromptVersion.project_id == project_id,
            PromptVersion.prompt_id == prompt_id,
            PromptVersion.version == version,
        )
    )


def protected_labels(db: Session, project: Project) -> list[str]:
    return sorted(
        db.scalars(select(ProtectedLabel.label).where(ProtectedLabel.project_id == project.id))
    )


def prompt_labels(db: Session, project: Project, prompt_id: str) -> dict[str, int]:
    """label -> version for one prompt, without the automatic `latest`."""
    rows = db.execute(
        select(PromptLabel.label, PromptLabel.version).where(
            PromptLabel.project_id == project.id, PromptLabel.prompt_id == prompt_id
        )
    )
    return {label: version for label, version in rows}


@dataclass(frozen=True)
class _Pending:
    """A version about to be written, as the include graph will see it once it is."""

    name: str
    type: str
    version: int
    content: Any
    labels: frozenset[str] = frozenset()  # labels that will point at it
    is_latest: bool = False  # `latest` will mean this version


def make_resolver(db: Session, project: Project, pending: _Pending | None = None) -> Resolver:
    """Resolve composition references against this project's prompts only.

    With `pending`, references resolve as they will after that version is saved or that label
    is moved, so a loop can be refused before anything is written.
    """

    def resolve(ref: Reference) -> Resolved | None:
        if pending is not None and ref.name == pending.name:
            hits = ref.version == pending.version or (
                ref.version is None
                and (ref.label in pending.labels or (ref.label == LATEST and pending.is_latest))
            )
            if hits:
                return Resolved(
                    pending.name, pending.version, pending.type, pending.content, ref.label
                )
        prompt = _prompt(db, project, ref.name)
        if prompt is None:
            return None
        if ref.version is not None:
            number = ref.version
        elif ref.label == LATEST:
            number = _latest_version(db, project.id, prompt.id)
        else:
            number = prompt_labels(db, project, prompt.id).get(ref.label or "")
        row = _version_row(db, project.id, prompt.id, number) if number else None
        if row is None:
            return None
        return Resolved(prompt.name, row.version, prompt.type, row.content, ref.label)

    return resolve


def _check_includes(db: Session, project: Project, pending: _Pending) -> list[dict[str, Any]]:
    """Resolve the whole include graph from `pending` as it will be after the write.

    Raises PromptError naming the reference for a missing include, a loop (including a prompt
    that includes itself, directly or through others) or nesting that is too deep. Returns the
    dependencies the version uses. Nothing is written, so nothing needs undoing.
    """
    try:
        _, dependencies = resolve_content(
            pending.type,
            pending.content,
            make_resolver(db, project, pending),
            root=(pending.name, pending.version),
        )
    except CompositionError as exc:
        raise PromptError(str(exc)) from exc
    return dependencies


# --- writes ---------------------------------------------------------------------------------


def create_version(
    db: Session,
    project: Project,
    actor: Actor,
    *,
    name: str,
    content: Any,
    commit_message: str,
    type: str | None = None,
    config: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    labels: list[str] | None = None,
) -> PromptVersion:
    """Save a new immutable version (v1 creates the prompt). Optionally point labels at it."""
    actor.require(Perm.PROMPTS_WRITE, "You need the Member role to save prompts.")
    name = validate_name(name)
    message = (commit_message or "").strip() if isinstance(commit_message, str) else ""
    if not message:
        raise PromptError("A commit message is required.")
    new_tags = None if tags is None else _tags(tags)
    config = _config(config)
    wanted = list(dict.fromkeys(validate_label(label) for label in (labels or [])))
    _check_label_rights(db, project, actor, wanted)

    existing = _prompt(db, project, name)
    prompt_type = existing.type if existing else type
    if prompt_type not in TYPES:
        raise PromptError("Prompt type must be `text` or `chat`.")
    if existing and type and type != existing.type:
        raise PromptError(f"{name!r} is a {existing.type} prompt; its type cannot change.")
    content = _content(prompt_type, content)
    # Check the include graph as it will be after this save (new `latest`, labels moved).
    dependencies = _check_includes(
        db,
        project,
        _Pending(
            name,
            prompt_type,
            (_latest_version(db, project.id, existing.id) if existing else 0) + 1,
            content,
            frozenset(wanted),
            is_latest=True,
        ),
    )

    now = utcnow()
    for attempt in range(3):  # two concurrent saves can race for the same version number
        try:
            with db.begin_nested():
                prompt = _prompt(db, project, name)
                created = prompt is None
                if prompt is None:
                    prompt = Prompt(
                        project_id=project.id,
                        name=name,
                        type=prompt_type,
                        tags=new_tags or [],
                        created_by=actor.id,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(prompt)
                    db.flush()
                elif new_tags is not None:
                    prompt.tags = new_tags
                prompt.updated_at = now
                row = PromptVersion(
                    project_id=project.id,
                    prompt_id=prompt.id,
                    version=_latest_version(db, project.id, prompt.id) + 1,
                    content=content,
                    config=config,
                    commit_message=message,
                    created_by=actor.id,
                    created_at=now,
                    dependencies=dependencies,
                )
                db.add(row)
                db.flush()
            break
        except IntegrityError:
            if attempt == 2:
                raise PromptError("Another save happened at the same time; try again.") from None

    _audit(
        db,
        project,
        actor,
        "prompt.created" if created else "prompt.version_created",
        name,
        {"prompt": name, "version": row.version, "type": prompt_type},
    )
    events.enqueue(
        db,
        project.id,
        events.VERSION_CREATED,
        {"prompt": name, "version": row.version, "actor": actor.label},
        now=now,
    )
    for label in wanted:
        _point_label(db, project, actor, prompt, label, row.version, now)
    return row


def move_label(
    db: Session, project: Project, actor: Actor, name: str, label: str, version: int
) -> bool:
    """Point `label` at `version`. Returns False when it already pointed there (no audit)."""
    actor.require(Perm.PROMPTS_WRITE, "You need the Member role to move labels.")
    validate_label(label)
    _check_label_rights(db, project, actor, [label])
    prompt = _require_prompt(db, project, name)
    row = _version_row(db, project.id, prompt.id, version)
    if row is None:
        raise PromptNotFound(f"Prompt {name!r} has no version {version}.")
    if prompt_labels(db, project, prompt.id).get(label) != version:
        # Refuse a move that would close a loop, before anything is written.
        _check_includes(
            db,
            project,
            _Pending(name, prompt.type, version, row.content, frozenset({label})),
        )
    return _point_label(db, project, actor, prompt, label, version, utcnow())


def remove_label(db: Session, project: Project, actor: Actor, name: str, label: str) -> bool:
    """Take `label` off the prompt. Protected labels need Admin+. False when it was not set."""
    actor.require(Perm.PROMPTS_WRITE, "You need the Member role to move labels.")
    validate_label(label)
    _check_label_rights(db, project, actor, [label])
    prompt = _require_prompt(db, project, name)
    return _point_label(db, project, actor, prompt, label, None, utcnow())


def set_protected(db: Session, project: Project, actor: Actor, label: str, protected: bool) -> bool:
    """Mark a label protected (Admin+ moves it from then on) or lift that. False if unchanged."""
    actor.require(Perm.PROMPT_LABELS_PROTECTED, "You need the Admin role to protect labels.")
    validate_label(label)
    current = db.get(ProtectedLabel, (project.id, label))
    if protected == (current is not None):
        return False
    if protected:
        db.add(ProtectedLabel(project_id=project.id, label=label))
    else:
        db.delete(current)
    db.flush()
    _audit(
        db,
        project,
        actor,
        "prompt.protected_label_changed",
        label,
        {"label": label, "protected": protected},
    )
    return True


def delete(db: Session, project: Project, actor: Actor, name: str) -> None:
    """Delete a prompt with all its versions and labels (Admin+, audited)."""
    actor.require(Perm.PROMPTS_DELETE, "You need the Admin role to delete prompts.")
    prompt = _require_prompt(db, project, name)
    versions = _latest_version(db, project.id, prompt.id)
    db.execute(sql_delete(PromptLabel).where(PromptLabel.prompt_id == prompt.id))
    db.execute(sql_delete(PromptVersion).where(PromptVersion.prompt_id == prompt.id))
    db.delete(prompt)
    db.flush()
    _audit(db, project, actor, "prompt.deleted", name, {"prompt": name, "versions": versions})


def _check_label_rights(db: Session, project: Project, actor: Actor, labels: list[str]) -> None:
    if not labels:
        return
    protected = set(protected_labels(db, project)) & set(labels)
    if protected and not actor.can(Perm.PROMPT_LABELS_PROTECTED):
        raise NotAllowed(
            f"Only an Admin can move the protected label {sorted(protected)[0]!r}."
            if actor.type == "user"
            else f"This API key may not move the protected label {sorted(protected)[0]!r}."
        )


def _point_label(
    db: Session,
    project: Project,
    actor: Actor,
    prompt: Prompt,
    label: str,
    version: int | None,
    now: datetime,
) -> bool:
    row = db.get(PromptLabel, (prompt.id, label))
    if row is not None and row.project_id != project.id:  # defence in depth; ids are unique
        raise PromptNotFound("Prompt not found.")
    before = row.version if row else None
    if before == version:
        return False
    if version is None:
        db.delete(row)
    elif row is None:
        db.add(
            PromptLabel(
                project_id=project.id,
                prompt_id=prompt.id,
                label=label,
                version=version,
                updated_by=actor.id,
                updated_at=now,
            )
        )
    else:
        row.version, row.updated_by, row.updated_at = version, actor.id, now
    db.flush()
    detail = {"prompt": prompt.name, "label": label, "from_version": before, "to_version": version}
    _audit(db, project, actor, "prompt.label_moved", prompt.name, detail)
    events.enqueue(
        db,
        project.id,
        events.LABEL_MOVED,
        {**detail, "actor": actor.label},
        now=now,
    )
    return True


def _audit(
    db: Session, project: Project, actor: Actor, action: str, target: str, detail: dict[str, Any]
) -> None:
    audit.record(
        db,
        action=action,
        actor_type=actor.type,
        actor_id=actor.id,
        org_id=project.org_id,
        project_id=project.id,
        target=target,
        detail=detail,
    )


# --- reads ----------------------------------------------------------------------------------


def get(
    db: Session,
    project: Project,
    actor: Actor,
    name: str,
    *,
    label: str | None = None,
    version: int | None = None,
    resolve: bool = True,
) -> PromptView:
    """One version of a prompt: by `label`, by `version`, or `production` (else `latest`)."""
    actor.require(Perm.PROMPTS_READ, "You cannot read prompts in this project.")
    if label is not None and version is not None:
        raise PromptError("Pass either a label or a version, not both.")
    prompt = _require_prompt(db, project, name)
    latest = _latest_version(db, project.id, prompt.id)
    labels = prompt_labels(db, project, prompt.id)
    if version is None:
        wanted = label if label is not None else PRODUCTION_LABEL
        if wanted == LATEST:
            version = latest
        else:
            version = labels.get(wanted)
            if version is None and label is None:
                version = latest  # no `production` yet: fall back to the newest version
            elif version is None:
                raise PromptNotFound(f"Prompt {name!r} has no label {label!r}.")
    row = _version_row(db, project.id, prompt.id, version)
    if row is None:
        raise PromptNotFound(f"Prompt {name!r} has no version {version}.")
    content, dependencies = row.content, row.dependencies
    if resolve:
        try:
            content, dependencies = resolve_content(
                prompt.type,
                row.content,
                make_resolver(db, project),
                root=(prompt.name, row.version),
            )
        except CompositionError as exc:
            raise PromptError(str(exc)) from exc
    shown = sorted(name_ for name_, v in labels.items() if v == row.version)
    if row.version == latest:
        shown.append(LATEST)
    return PromptView(
        prompt_id=prompt.id,
        name=prompt.name,
        type=prompt.type,
        version=row.version,
        prompt=content,
        config=row.config,
        labels=shown,
        tags=list(prompt.tags or []),
        commit_message=row.commit_message,
        dependencies=dependencies,
        created_by=row.created_by,
        created_at=row.created_at,
        latest_version=latest,
    )


def versions(db: Session, project: Project, actor: Actor, name: str) -> list[PromptVersion]:
    """All versions, newest first."""
    actor.require(Perm.PROMPTS_READ, "You cannot read prompts in this project.")
    prompt = _require_prompt(db, project, name)
    return list(
        db.scalars(
            select(PromptVersion)
            .where(PromptVersion.project_id == project.id, PromptVersion.prompt_id == prompt.id)
            .order_by(PromptVersion.version.desc())
        )
    )


def list_prompts(
    db: Session,
    project: Project,
    actor: Actor,
    *,
    folder: str = "",
    direct_only: bool = False,
    search: str = "",
    name: str = "",
    tag: str | None = None,
    label: str | None = None,
    sort: str = "name",
    descending: bool = False,
    page: int = 1,
    limit: int = 50,
) -> PromptList:
    """Prompts of the project, filtered and sorted.

    `name` is a case-insensitive substring of the name alone. `folder` limits to that folder's
    subtree (`direct_only`: just its own prompts). `search` is a case-insensitive substring over
    name, tags and the latest version's content.
    `folders` lists the immediate sub-folders of `folder` among the matches.
    """
    actor.require(Perm.PROMPTS_READ, "You cannot read prompts in this project.")
    if sort not in SORTS:
        raise PromptError(f"Sort by one of: {', '.join(SORTS)}.")
    page, limit = max(1, page), min(max(1, limit), 200)
    folder = folder.strip("/")

    stmt = select(Prompt).where(Prompt.project_id == project.id)
    if folder:
        stmt = stmt.where(Prompt.name.startswith(f"{folder}/", autoescape=True))
    if label:
        labelled = select(PromptLabel.prompt_id).where(
            PromptLabel.project_id == project.id, PromptLabel.label == label
        )
        if label == LATEST:
            labelled = select(Prompt.id).where(Prompt.project_id == project.id)
        stmt = stmt.where(Prompt.id.in_(labelled))
    prompts = list(db.scalars(stmt))
    if name.strip():
        prompts = [p for p in prompts if name.strip().lower() in p.name.lower()]
    if tag:
        prompts = [p for p in prompts if tag in (p.tags or [])]
    needle = search.strip().lower()
    snippets: dict[str, str] = {}
    if needle:
        kept = []
        for p in prompts:
            hit, snippet = _match(db, project.id, p, needle)
            if hit:
                kept.append(p)
                snippets[p.id] = snippet
        prompts = kept

    ids = [p.id for p in prompts]
    latest = dict(
        db.execute(
            select(PromptVersion.prompt_id, func.max(PromptVersion.version))
            .where(PromptVersion.project_id == project.id, PromptVersion.prompt_id.in_(ids))
            .group_by(PromptVersion.prompt_id)
        ).all()
    )
    authors = {
        (prompt_id, version): created_by
        for prompt_id, version, created_by in db.execute(
            select(PromptVersion.prompt_id, PromptVersion.version, PromptVersion.created_by).where(
                PromptVersion.project_id == project.id,
                PromptVersion.prompt_id.in_(ids),
                PromptVersion.version.in_({v for v in latest.values()} or {0}),
            )
        )
    }
    labels: dict[str, dict[str, int]] = {}
    for row in db.scalars(
        select(PromptLabel).where(
            PromptLabel.project_id == project.id, PromptLabel.prompt_id.in_(ids)
        )
    ):
        labels.setdefault(row.prompt_id, {})[row.label] = row.version
    summaries = [
        PromptSummary(
            p.name,
            p.type,
            list(p.tags or []),
            latest.get(p.id, 0),
            labels.get(p.id, {}),
            p.updated_at,
            authors.get((p.id, latest.get(p.id, 0))),
            snippets.get(p.id, ""),
        )
        for p in prompts
    ]

    prefix = f"{folder}/" if folder else ""
    children: Counter[str] = Counter()
    for s in summaries:
        rest = s.name[len(prefix) :]
        if "/" in rest:
            children[prefix + rest.split("/", 1)[0]] += 1
    if direct_only:
        summaries = [s for s in summaries if "/" not in s.name[len(prefix) :]]
    key = (lambda s: s.updated_at) if sort == "updated" else (lambda s: s.name.lower())
    summaries.sort(key=key, reverse=descending)
    return PromptList(
        items=summaries[(page - 1) * limit : page * limit],
        total=len(summaries),
        folders=sorted(children.items()),
        page=page,
        limit=limit,
    )


def _match(db: Session, project_id: str, prompt: Prompt, needle: str) -> tuple[bool, str]:
    """(matched, snippet). The snippet is the matching content line, only when the name and
    tags did not already explain the match."""
    if needle in prompt.name.lower() or any(needle in t.lower() for t in prompt.tags or []):
        return True, ""
    latest = db.scalar(
        select(PromptVersion.content)
        .where(PromptVersion.project_id == project_id, PromptVersion.prompt_id == prompt.id)
        .order_by(PromptVersion.version.desc())
        .limit(1)
    )
    if latest is None:
        return False, ""
    text = latest if isinstance(latest, str) else "\n".join(m.get("content", "") for m in latest)
    for line in text.splitlines():
        if needle in line.lower():
            return True, line.strip()[:200]
    return False, ""


def facets(db: Session, project: Project, actor: Actor) -> tuple[list[str], list[str]]:
    """(tags, labels) in use across the project's prompts, for filter menus."""
    actor.require(Perm.PROMPTS_READ, "You cannot read prompts in this project.")
    tags = {
        t
        for (row,) in db.execute(select(Prompt.tags).where(Prompt.project_id == project.id))
        for t in row or []
    }
    labels = set(db.scalars(select(PromptLabel.label).where(PromptLabel.project_id == project.id)))
    return sorted(tags), sorted(labels)
