"""Judge rules for the web UI (FR-J3): validation, writes with audit rows, the model a rule will
use, a preview of what the judge would receive, and the figures the rules page shows.

Functions take the project and flush but leave the commit to the caller. Permission checks
(Perm.EVAL_RULES) belong to the route, as for the templates. Evaluation of the rule itself lives
in `evals.rules` and the runner."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from traceloom.config import Settings
from traceloom.cost.registry import ModelDef, ModelRegistry
from traceloom.db.base import utcnow
from traceloom.db.models import LLMConnection, Project, ScoreConfig, User
from traceloom.db.models_eval import EvalExecution, EvalRule, EvalTemplateVersion
from traceloom.evals import mapping as mapping_mod
from traceloom.evals import spend, templates
from traceloom.evals.rules import OBSERVATION, TRACE, RuleSnapshot, matches
from traceloom.ports import ObservationRow, TraceDetail, TraceRow
from traceloom.prompts.variables import find_variables
from traceloom.services import audit

NAME_MAX = 200
MAX_DELAY_SECONDS = 86_400
TARGET_TYPE_RE = re.compile(r"[A-Za-z_]{1,32}")
SOURCES = ("input", "output", "expected_output", "metadata", "observation")
FILTER_FIELDS = ("name", "tags", "environment", "user_id", "session_id", "release", "version")
PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "azure_openai": "Azure OpenAI",
    "bedrock": "Amazon Bedrock",
    "google": "Google",
    "openai_compatible": "an OpenAI-compatible endpoint",
}


class RuleError(ValueError):
    """The rule cannot be saved; the message is safe to show."""

    def __init__(self, message: str, field_name: str = "") -> None:
        super().__init__(message)
        self.field = field_name


class RuleNotFound(LookupError):
    pass


@dataclass
class RuleForm:
    """A rule as the editor submits it, before validation."""

    name: str = ""
    template_id: str = ""
    template_version: str = ""  # "" follows the latest
    score_name: str = ""
    config_id: str = ""
    target: str = TRACE
    target_name: str = ""
    target_type: str = ""
    filter: dict[str, Any] = field(default_factory=dict)
    sampling_pct: str = "100"
    delay_seconds: str = "30"
    mapping: dict[str, dict[str, str]] = field(default_factory=dict)
    connection_id: str = ""
    model: str = ""


# --- Reads -------------------------------------------------------------------------------------


def get_rule(db: Session, project_id: str, rule_id: str) -> EvalRule:
    rule = db.scalar(
        select(EvalRule).where(EvalRule.id == rule_id, EvalRule.project_id == project_id)
    )
    if rule is None:
        raise RuleNotFound(rule_id)
    return rule


def list_rules(db: Session, project_id: str) -> list[EvalRule]:
    return list(
        db.scalars(
            select(EvalRule)
            .where(EvalRule.project_id == project_id)
            .order_by(EvalRule.created_at, EvalRule.id)
        )
    )


def outcomes_24h(db: Session, project_id: str, now: datetime | None = None) -> dict[str, Counter]:
    """rule id -> status counts over the last 24 hours."""
    since = (now or utcnow()) - timedelta(hours=24)
    rows = db.execute(
        select(EvalExecution.rule_id, EvalExecution.status, func.count())
        .where(EvalExecution.project_id == project_id, EvalExecution.created_at >= since)
        .where(EvalExecution.rule_id.is_not(None))
        .group_by(EvalExecution.rule_id, EvalExecution.status)
    )
    out: dict[str, Counter] = {}
    for rule_id, status, count in rows:
        out.setdefault(rule_id, Counter())[status] = count
    return out


@dataclass(frozen=True)
class Model:
    connection_id: str | None
    connection_name: str
    provider: str  # display label
    model: str | None

    @property
    def chosen(self) -> bool:
        return bool(self.connection_id and self.model)


def effective_model(
    db: Session,
    project_id: str,
    rule_connection: str | None,
    rule_model: str | None,
    version: EvalTemplateVersion,
) -> Model:
    """The template version's model, else the one chosen on the rule."""
    connection_id = version.connection_id or rule_connection
    model = version.model or rule_model
    row = db.get(LLMConnection, connection_id) if connection_id else None
    if row is None or row.project_id != project_id:
        return Model(None, "", "", model)
    return Model(row.id, row.name, PROVIDER_LABELS.get(row.provider, row.provider), model)


def model_def(
    registry: ModelRegistry | None, project_id: str, model: str | None, now: datetime | None = None
) -> ModelDef | None:
    if registry is None or not model:
        return None
    return registry.find(project_id, model, now or utcnow())


def per_call_estimate(
    registry: ModelRegistry | None, project_id: str, version: EvalTemplateVersion, model: str | None
) -> spend.Estimate:
    """What one judge call may cost: the template prompt as the input, max_tokens as the output."""
    definition = model_def(registry, project_id, model)
    tokens = spend.count_prompt_tokens(definition, version.prompt)
    return spend.estimate_cost(definition, tokens, (version.params or {}).get("max_tokens"))


# --- Validation and writes ---------------------------------------------------------------------


def _number(raw: str, label: str, low: float, high: float, integer: bool = False) -> float:
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise RuleError(f"{label} must be a number.", label.lower()) from None
    if not low <= value <= high:
        raise RuleError(f"{label} must be between {low:g} and {high:g}.", label.lower())
    if integer and value != int(value):
        raise RuleError(f"{label} must be a whole number.", label.lower())
    return value


def _clean_filter(raw: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in FILTER_FIELDS:
        value = raw.get(key)
        if key == "tags":
            tags = [str(t).strip() for t in (value or []) if str(t).strip()]
            if tags:
                clean["tags"] = tags
        elif value is not None and str(value).strip():
            clean[key] = str(value).strip()
    metadata = {
        str(k).strip(): str(v) for k, v in (raw.get("metadata") or {}).items() if str(k).strip()
    }
    if metadata:
        clean["metadata"] = metadata
    return clean


def _clean_mapping(variables: list[str], raw: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """One validated entry per template variable. A variable left on its own name
    (input, output, expected_output) with no path and no default needs no entry."""
    clean: dict[str, Any] = {}
    for name in variables:
        spec = raw.get(name) or {}
        source = (spec.get("source") or "").strip() or (
            name if name in mapping_mod.IMPLICIT_SOURCES else ""
        )
        path, default = (spec.get("path") or "").strip(), spec.get("default") or ""
        if not source:
            raise RuleError(f"Choose a source for {{{{{name}}}}}.", f"map_{name}")
        entry: dict[str, Any] = {}
        if source == "metadata":
            key = (spec.get("key") or "").strip()
            if not key:
                raise RuleError(f"Enter the metadata key for {{{{{name}}}}}.", f"map_{name}")
            entry["source"] = f"metadata.{key}"
        elif source == "observation":
            obs = (spec.get("observation") or "").strip()
            if not obs:
                raise RuleError("Choose an observation name.", f"map_{name}")
            entry |= {"source": "observation", "observation": obs}
            field_ = (spec.get("field") or "output").strip()
            entry["field"] = field_ if field_ in mapping_mod.OBSERVATION_FIELDS else "output"
        elif source in mapping_mod.IMPLICIT_SOURCES:
            entry["source"] = source
        else:
            raise RuleError(f"Unknown source for {{{{{name}}}}}.", f"map_{name}")
        if path:
            try:
                mapping_mod.parse_path(path)
            except mapping_mod.PathError as exc:
                raise RuleError(str(exc), f"map_{name}") from exc
            entry["path"] = path
        if default != "":
            entry["default"] = default
        if entry != {"source": name} or path or default != "":
            clean[name] = entry
    return clean


def validate(db: Session, project_id: str, form: RuleForm) -> dict[str, Any]:
    """The cleaned column values for a rule, or RuleError naming the first bad field."""
    try:
        template = templates.get_template(db, project_id, form.template_id)
    except templates.TemplateNotFound:
        raise RuleError("Choose a template.", "template_id") from None
    pinned: int | None = None
    if form.template_version.strip():
        pinned = int(_number(form.template_version, "Version", 1, 1_000_000, integer=True))
    try:
        version = templates.get_version(db, template.id, pinned)
    except templates.TemplateNotFound:
        raise RuleError("That template version does not exist.", "template_version") from None

    name = form.name.strip() or template.name
    if len(name) > NAME_MAX:
        raise RuleError(f"The name is at most {NAME_MAX} characters.", "name")
    score_name = form.score_name.strip() or template.name
    if len(score_name) > NAME_MAX:
        raise RuleError(f"The score name is at most {NAME_MAX} characters.", "score_name")

    config_id = form.config_id.strip() or None
    if config_id:
        config = db.get(ScoreConfig, config_id)
        if config is None or config.project_id != project_id or config.archived_at is not None:
            raise RuleError("Choose an active score config, or none.", "config_id")

    if form.target not in (TRACE, OBSERVATION):
        raise RuleError("Choose what the rule judges.", "target")
    target_name = form.target_name.strip() or None
    target_type = form.target_type.strip() or None
    if form.target == TRACE:
        target_type = None
    elif target_type and not TARGET_TYPE_RE.fullmatch(target_type):
        raise RuleError("The observation type is letters and underscores only.", "target_type")
    if form.target == OBSERVATION and not (target_name or target_type):
        raise RuleError("Name the observation or choose its type.", "target_name")
    if form.target == OBSERVATION and target_type:
        target_type = target_type.upper()

    connection_id, model = form.connection_id.strip() or None, form.model.strip() or None
    if version.model:  # the template decides; the rule keeps no override
        connection_id = model = None
    else:
        if not (connection_id and model):
            raise RuleError("This template names no model. Choose a connection and model.", "model")
        conn = db.get(LLMConnection, connection_id)
        if conn is None or conn.project_id != project_id:
            raise RuleError("Choose one of the project's LLM connections.", "connection_id")

    return {
        "name": name,
        "template_id": template.id,
        "template_version": pinned,
        "score_name": score_name,
        "config_id": config_id,
        "target": form.target,
        "target_name": target_name,
        "target_type": target_type,
        "filter": _clean_filter(form.filter),
        "sampling_pct": _number(form.sampling_pct, "Sampling", 0, 100),
        "delay_seconds": int(
            _number(form.delay_seconds, "Delay", 0, MAX_DELAY_SECONDS, integer=True)
        ),
        "mapping": _clean_mapping(find_variables(version.prompt), form.mapping),
        "connection_id": connection_id,
        "model": model,
    }


def _audit(db: Session, project: Project, user: User, action: str, rule: EvalRule, **detail: Any):
    audit.record(
        db,
        action=action,
        actor_type="user",
        actor_id=user.id,
        org_id=project.org_id,
        project_id=project.id,
        target=rule.id,
        detail={"name": rule.name, **detail},
    )


def create_rule(db: Session, project: Project, user: User, form: RuleForm) -> EvalRule:
    """A new rule starts disabled: enabling asks for the provider confirmation."""
    values = validate(db, project.id, form)
    rule = EvalRule(
        project_id=project.id,
        enabled=False,
        created_by=user.id,
        created_at=utcnow(),
        updated_at=utcnow(),
        **values,
    )
    db.add(rule)
    db.flush()
    _audit(db, project, user, "eval_rule.create", rule, template_id=rule.template_id)
    return rule


def update_rule(
    db: Session, project: Project, user: User, rule_id: str, form: RuleForm
) -> EvalRule:
    rule = get_rule(db, project.id, rule_id)
    changed = validate(db, project.id, form)
    before = {k: getattr(rule, k) for k in changed}
    for key, value in changed.items():
        setattr(rule, key, value)
    rule.updated_at = utcnow()
    db.flush()
    fields = sorted(k for k in changed if before[k] != changed[k])
    _audit(db, project, user, "eval_rule.update", rule, changed=fields)
    return rule


def set_enabled(
    db: Session, project: Project, user: User, rule_id: str, enabled: bool, confirmed: bool
) -> EvalRule:
    """Enabling needs the confirmation (the user saw which provider and model get trace content)
    and a model to call."""
    rule = get_rule(db, project.id, rule_id)
    if enabled and not rule.enabled:
        if not confirmed:
            raise RuleError("Confirm that trace content may be sent to the model provider.")
        version = templates.get_version(db, rule.template_id, rule.template_version)
        if not effective_model(db, project.id, rule.connection_id, rule.model, version).chosen:
            raise RuleError("Choose a connection and model before enabling this rule.", "model")
    if rule.enabled != enabled:
        rule.enabled = enabled
        rule.updated_at = utcnow()
        db.flush()
        _audit(db, project, user, "eval_rule.enable" if enabled else "eval_rule.disable", rule)
    return rule


def delete_rule(db: Session, project: Project, user: User, rule_id: str) -> None:
    rule = get_rule(db, project.id, rule_id)
    _audit(db, project, user, "eval_rule.delete", rule)
    db.delete(rule)
    db.flush()


def audit_backfill(
    db: Session, project: Project, user: User, rule: EvalRule, created: int, capped: bool
) -> None:
    _audit(db, project, user, "eval_rule.backfill", rule, created=created, capped=capped)


# --- Preview -----------------------------------------------------------------------------------


@dataclass
class Preview:
    trace_id: str | None = None
    values: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    found: int = 0  # recent matches scanned


def _context(
    row: TraceRow | ObservationRow, detail: TraceDetail | None
) -> mapping_mod.ObjectContext:
    observations = [
        {"name": o.name, "input": o.input, "output": o.output}
        for o in sorted(detail.observations if detail else [], key=lambda o: o.start_time)
        if o.id != row.id
    ]
    return mapping_mod.ObjectContext(
        input=row.input, output=row.output, metadata=row.metadata or {}, observations=observations
    )


def preview(
    store: Any,
    settings: Settings,
    project_id: str,
    rule: RuleSnapshot,
    variables: list[str],
    mapping: Mapping[str, Any],
    skip: int = 0,
    scan: int = 50,
) -> Preview:
    """What the judge would receive for the skip-th most recent trace the rule matches. No model
    call is made."""
    from traceloom.evals import rules as rules_mod
    from traceloom.evals.backfill import rule_query

    skipped = rules_mod.excluded_environments(settings)
    page = store.list_traces(project_id, rule_query(rule, None, None, None))
    matched: list[tuple[TraceRow | ObservationRow, TraceDetail | None]] = []
    for trace in page.items[:scan]:
        if trace.environment in skipped:
            continue
        detail = (
            store.get_trace(project_id, trace.id)
            if rule.target == OBSERVATION or variables
            else None
        )
        candidates = detail.observations if rule.target == OBSERVATION and detail else [trace]
        matched += [(c, detail) for c in candidates if matches(rule, c)]
        if len(matched) > skip:
            break
    result = Preview(found=len(matched))
    if len(matched) <= skip:
        return result
    row, detail = matched[skip]
    result.trace_id = row.trace_id if isinstance(row, ObservationRow) else row.id
    context = _context(row, detail)
    for name in variables:
        try:
            result.values.update(mapping_mod.resolve_variables([name], mapping, context))
        except mapping_mod.MappingError as exc:
            result.errors[name] = str(exc)
    return result


def parse_utc(value: str, end_of_day: bool = False) -> datetime | None:
    """A date (YYYY-MM-DD) or ISO timestamp from a form, as UTC; None when blank or invalid."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        if len(value) == 10:
            day = datetime.fromisoformat(value).replace(tzinfo=UTC)
            return day + timedelta(days=1, microseconds=-1) if end_of_day else day
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
