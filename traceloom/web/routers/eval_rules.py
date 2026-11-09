"""Evaluation > Evaluators > Rules: the list with its cap banner, the rule editor with a preview,
the enable confirmation and backfill (Admin+; everyone with eval access can read) (FR-J3, J6)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from traceloom.auth.deps import Access, form_data, get_container, require
from traceloom.auth.permissions import Perm, can
from traceloom.container import Container
from traceloom.db.models import ScoreConfig
from traceloom.db.models_eval import EvalRule
from traceloom.db.session import get_db
from traceloom.evals import backfill as backfill_mod
from traceloom.evals import rule_service as svc
from traceloom.evals import rules as rules_mod
from traceloom.evals import spend
from traceloom.evals import templates as tpl
from traceloom.llm.connections import ConnectionPolicy, list_usable
from traceloom.prompts.service import Actor
from traceloom.prompts.variables import find_variables
from traceloom.web.routers.evaluators import score_label, shell
from traceloom.web.templating import render

router = APIRouter(tags=["eval-rules"])

NOTES = {
    "created": "Rule saved. It is off until you enable it.",
    "saved": "Rule saved.",
    "enabled": "Rule enabled. New matching traces are judged after the rule's delay.",
    "disabled": "Rule disabled.",
    "deleted": "Rule deleted.",
}
SOURCE_LABELS = [
    ("input", "Input"),
    ("output", "Output"),
    ("metadata", "Metadata key"),
    ("observation", "Child observation"),
    ("expected_output", "Expected output (datasets only)"),
]
FILTER_LABELS = [
    ("name", "name"),
    ("tags", "tags"),
    ("environment", "environment"),
    ("user_id", "user id"),
    ("session_id", "session id"),
    ("release", "release"),
    ("version", "version"),
    ("metadata", "metadata"),
]


def _base(access: Access) -> str:
    return f"/project/{access.project.id}/evals/rules"


def _describe_target(rule: EvalRule) -> dict[str, str]:
    """The parts of "Traces named X" / "Observations of type T named N"."""
    if rule.target == "trace":
        name = (rule.filter or {}).get("name")
        return {"kind": "Traces named" if name else "All traces", "name": name or "", "type": ""}
    kind = "Observations"
    if rule.target_type:
        kind += " of type"
    return {"kind": kind, "type": rule.target_type or "", "name": rule.target_name or ""}


# --- List --------------------------------------------------------------------------------------


@router.get("/project/{project_id}/evals/rules", response_class=HTMLResponse)
def rules_page(
    request: Request,
    notice: str = "",
    confirm: str = "",
    error: str = "",
    info: str = "",
    access: Access = Depends(require(Perm.EVAL_READ)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    project = access.project
    outcomes = svc.outcomes_24h(db, project.id)
    status = spend.status(db, project.id, container.settings)
    rows = []
    for rule in svc.list_rules(db, project.id):
        template = db.get(tpl.EvalTemplate, rule.template_id)
        version = tpl.get_version(db, rule.template_id, rule.template_version)
        model = svc.effective_model(db, project.id, rule.connection_id, rule.model, version)
        estimate = svc.per_call_estimate(container.models, project.id, version, model.model)
        rows.append(
            {
                "rule": rule,
                "title": rule.name or rule.score_name,
                "template": template.name,
                "builtin": template.project_id is None,
                "pinned": rule.template_version,
                "latest": version.version,
                "target": _describe_target(rule),
                "model": model,
                "estimate": estimate,
                "outcome": outcomes.get(rule.id, {}),
            }
        )
    cap_banner = None
    if status.reached:
        cap_banner = {"paused": status.paused, "cap": status.cap}
    context = shell(
        db,
        access,
        "rules",
        rows=rows,
        notice=NOTES.get(notice, "") or info[:300],
        error=error[:300],
        confirm=confirm,
        cap_banner=cap_banner,
        spend=status,
        is_admin=can(access.role, Perm.EVAL_RULES),
        can_settings=can(access.role, Perm.EVAL_SETTINGS),
        backfill_max=container.settings.eval_backfill_max,
    )
    return render(request, "evals/rules.html", context)


# --- Editor ------------------------------------------------------------------------------------


def _form_from_rule(rule: EvalRule) -> svc.RuleForm:
    mapping: dict[str, dict[str, str]] = {}
    for name, spec in (rule.mapping or {}).items():
        source = spec.get("source", "")
        entry = {"path": spec.get("path", ""), "default": str(spec.get("default", ""))}
        if source.startswith("metadata."):
            entry |= {"source": "metadata", "key": source[len("metadata.") :]}
        else:
            entry |= {
                "source": source,
                "observation": spec.get("observation", ""),
                "field": spec.get("field", "output"),
            }
        mapping[name] = entry
    flt = dict(rule.filter or {})
    return svc.RuleForm(
        name=rule.name,
        template_id=rule.template_id,
        template_version="" if rule.template_version is None else str(rule.template_version),
        score_name=rule.score_name,
        config_id=rule.config_id or "",
        target=rule.target,
        target_name=rule.target_name or "",
        target_type=rule.target_type or "",
        filter=flt,
        sampling_pct=f"{rule.sampling_pct:g}",
        delay_seconds=str(rule.delay_seconds),
        mapping=mapping,
        connection_id=rule.connection_id or "",
        model=rule.model or "",
    )


def _filter_rows(flt: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for key, _label in FILTER_LABELS:
        value = flt.get(key)
        if key == "tags" and value:
            rows.append({"field": key, "value": ", ".join(value)})
        elif key == "metadata":
            rows += [{"field": key, "value": f"{k}={v}"} for k, v in (value or {}).items()]
        elif value:
            rows.append({"field": key, "value": str(value)})
    return rows


def parse_form(form: FormData, variables: list[str] | None = None) -> svc.RuleForm:
    """The editor's fields as a RuleForm. Mapping rows are read for `variables` only."""
    target = str(form.get("target", "trace"))
    flt: dict[str, Any] = {}
    tags: list[str] = []
    metadata: dict[str, str] = {}
    for field_name, value in zip(form.getlist("f_field"), form.getlist("f_value"), strict=False):
        field_name, value = str(field_name), str(value).strip()
        if not value:
            continue
        if field_name == "tags":
            tags += [t.strip() for t in value.split(",") if t.strip()]
        elif field_name == "metadata":
            key, _, val = value.partition("=")
            if key.strip():
                metadata[key.strip()] = val.strip()
        elif field_name in svc.FILTER_FIELDS:
            flt[field_name] = value
    if tags:
        flt["tags"] = tags
    if metadata:
        flt["metadata"] = metadata
    trace_name = str(form.get("trace_name", "")).strip()
    if target == "trace" and trace_name:
        flt["name"] = trace_name
    mapping = {}
    for name in variables or []:
        mapping[name] = {
            part: str(form.get(f"map_{name}_{part}", ""))
            for part in ("source", "key", "observation", "field", "path", "default")
        }
    get = lambda k: str(form.get(k, "")).strip()  # noqa: E731
    return svc.RuleForm(
        name=get("name"),
        template_id=get("template_id"),
        template_version=get("template_version"),
        score_name=get("score_name"),
        config_id=get("config_id"),
        target=target,
        target_name=get("observation_name"),
        target_type=get("observation_type"),
        filter=flt,
        sampling_pct=get("sampling_pct") or "100",
        delay_seconds=get("delay_seconds") or "30",
        mapping=mapping,
        connection_id=get("connection_id"),
        model=get("model"),
    )


def _template_choices(db: Session, project_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": v.template.id,
            "name": v.template.name,
            "builtin": v.builtin,
            "latest": v.latest.version,
            "has_model": bool(v.latest.model),
        }
        for v in tpl.list_templates(db, project_id)
    ]


def _editor(
    request: Request,
    db: Session,
    access: Access,
    container: Container,
    form: svc.RuleForm,
    *,
    rule: EvalRule | None = None,
    error: svc.RuleError | None = None,
    status: int = 200,
):
    project = access.project
    choices = _template_choices(db, project.id)
    if not form.template_id and choices:
        form.template_id = choices[0]["id"]
    version, variables, template = None, [], None
    try:
        template = tpl.get_template(db, project.id, form.template_id)
        pinned = int(form.template_version) if form.template_version.strip().isdigit() else None
        version = tpl.get_version(db, template.id, pinned)
        variables = find_variables(version.prompt)
    except tpl.TemplateNotFound:
        pass
    if not form.score_name and template:
        form.score_name = template.name
    actor = Actor.for_user(access.user, access.role)
    connections = list_usable(
        db, project, actor, ConnectionPolicy.from_settings(container.settings)
    )
    configs = [
        {"id": c.id, "name": c.name, "type": c.data_type.lower()}
        for c in db.query(ScoreConfig)
        .filter(ScoreConfig.project_id == project.id, ScoreConfig.archived_at.is_(None))
        .order_by(ScoreConfig.name)
    ]
    model = (
        svc.effective_model(db, project.id, form.connection_id or None, form.model or None, version)
        if version
        else None
    )
    context = shell(
        db,
        access,
        "rules",
        rule=rule,
        form=form,
        error=error,
        choices=choices,
        template=template,
        version=version,
        variables=variables,
        score=score_label(version.output_schema) if version else "",
        needs_model=bool(version and not version.model),
        connections=[{"id": c.id, "name": c.name, "models": c.models} for c in connections],
        configs=configs,
        sources=SOURCE_LABELS,
        filter_fields=FILTER_LABELS,
        filter_rows=_filter_rows(form.filter) or [{"field": "tags", "value": ""}],
        trace_name=form.filter.get("name", ""),
        model=model,
    )
    return render(request, "evals/rule_edit.html", context, status=status)


def _variables_for(db: Session, project_id: str, form: svc.RuleForm) -> list[str]:
    try:
        template = tpl.get_template(db, project_id, form.template_id)
        pinned = int(form.template_version) if form.template_version.strip().isdigit() else None
        return find_variables(tpl.get_version(db, template.id, pinned).prompt)
    except tpl.TemplateNotFound:
        return []


@router.get("/project/{project_id}/evals/rules/new", response_class=HTMLResponse)
def new_rule_page(
    request: Request,
    template_id: str = "",
    access: Access = Depends(require(Perm.EVAL_RULES)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    return _editor(request, db, access, container, svc.RuleForm(template_id=template_id))


@router.get("/project/{project_id}/evals/rules/preview", response_class=HTMLResponse)
def preview_rule(
    request: Request,
    skip: int = 0,
    access: Access = Depends(require(Perm.EVAL_RULES)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    """The values the judge would receive for a recent matching trace (no model call)."""
    form_values = FormData(list(request.query_params.multi_items()))
    variables = _variables_for(db, access.project.id, parse_form(form_values))
    form = parse_form(form_values, variables)
    error = ""
    result = svc.Preview()
    try:
        values = svc.validate(db, access.project.id, form)
        snapshot = rules_mod.RuleSnapshot(
            "preview",
            access.project.id,
            values["target"],
            values["target_name"],
            values["target_type"],
            values["filter"],
            100.0,
            0,
            "",
        )
        result = svc.preview(
            container.trace_store,
            container.settings,
            access.project.id,
            snapshot,
            variables,
            values["mapping"],
            max(0, skip),
        )
    except svc.RuleError as exc:
        error = str(exc)
    context = {"preview": result, "variables": variables, "error": error, "skip": max(0, skip)}
    return render(request, "evals/_preview.html", context)


@router.get("/project/{project_id}/evals/rules/{rule_id}", response_class=HTMLResponse)
def edit_rule_page(
    request: Request,
    rule_id: str,
    access: Access = Depends(require(Perm.EVAL_RULES)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    try:
        rule = svc.get_rule(db, access.project.id, rule_id)
    except svc.RuleNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    return _editor(request, db, access, container, _form_from_rule(rule), rule=rule)


def _save(
    request: Request,
    access: Access,
    container: Container,
    form_values: FormData,
    db: Session,
    rule_id: str | None,
):
    base = svc.RuleForm(
        template_id=str(form_values.get("template_id", "")),
        template_version=str(form_values.get("template_version", "")),
    )
    form = parse_form(form_values, _variables_for(db, access.project.id, base))
    rule = None
    try:
        if rule_id:
            rule = svc.update_rule(db, access.project, access.user, rule_id, form)
        else:
            rule = svc.create_rule(db, access.project, access.user, form)
    except svc.RuleNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except svc.RuleError as exc:
        db.rollback()
        existing = svc.get_rule(db, access.project.id, rule_id) if rule_id else None
        return _editor(request, db, access, container, form, rule=existing, error=exc, status=400)
    db.commit()
    rules_mod.invalidate_rules(container, access.project.id)
    if rule_id:
        return RedirectResponse(f"{_base(access)}?notice=saved", status_code=303)
    return RedirectResponse(f"{_base(access)}?notice=created&confirm={rule.id}", status_code=303)


@router.post("/project/{project_id}/evals/rules", response_class=HTMLResponse)
def create_rule(
    request: Request,
    access: Access = Depends(require(Perm.EVAL_RULES)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    return _save(request, access, container, form, db, None)


@router.post("/project/{project_id}/evals/rules/{rule_id}", response_class=HTMLResponse)
def update_rule(
    request: Request,
    rule_id: str,
    access: Access = Depends(require(Perm.EVAL_RULES)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    return _save(request, access, container, form, db, rule_id)


# --- Enable, delete, backfill -------------------------------------------------------------------


@router.post("/project/{project_id}/evals/rules/{rule_id}/toggle")
def toggle_rule(
    rule_id: str,
    access: Access = Depends(require(Perm.EVAL_RULES)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    """Disable, or enable once the confirmation (`confirm=1`) says the user saw the provider."""
    enabled = str(form.get("enabled", "")) == "1"
    try:
        svc.set_enabled(
            db, access.project, access.user, rule_id, enabled, str(form.get("confirm", "")) == "1"
        )
    except svc.RuleNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    except svc.RuleError as exc:
        db.rollback()
        return RedirectResponse(f"{_base(access)}?error={_quote(str(exc))}", status_code=303)
    db.commit()
    rules_mod.invalidate_rules(container, access.project.id)
    return RedirectResponse(
        f"{_base(access)}?notice={'enabled' if enabled else 'disabled'}", status_code=303
    )


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


@router.post("/project/{project_id}/evals/rules/{rule_id}/delete")
def delete_rule(
    rule_id: str,
    access: Access = Depends(require(Perm.EVAL_RULES)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    try:
        svc.delete_rule(db, access.project, access.user, rule_id)
    except svc.RuleNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    db.commit()
    rules_mod.invalidate_rules(container, access.project.id)
    return RedirectResponse(f"{_base(access)}?notice=deleted", status_code=303)


@router.post("/project/{project_id}/evals/rules/{rule_id}/backfill")
def backfill_rule(
    rule_id: str,
    access: Access = Depends(require(Perm.EVAL_RULES)),
    container: Container = Depends(get_container),
    form: FormData = Depends(form_data),
    db: Session = Depends(get_db),
):
    try:
        rule = svc.get_rule(db, access.project.id, rule_id)
    except svc.RuleNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    result = backfill_mod.backfill(
        container,
        access.project.id,
        rule_id,
        svc.parse_utc(str(form.get("from", ""))),
        svc.parse_utc(str(form.get("to", "")), end_of_day=True),
    )
    svc.audit_backfill(db, access.project, access.user, rule, result.created, result.capped)
    db.commit()
    detail = f"Backfill queued {result.created} execution{'' if result.created == 1 else 's'}."
    if result.capped:
        limit = container.settings.eval_backfill_max
        detail += f" It stopped at the limit of {limit}; run it again to continue."
    return RedirectResponse(f"{_base(access)}?info={_quote(detail)}", status_code=303)


@router.get("/project/{project_id}/evals/rules/{rule_id}/backfill-estimate")
def backfill_estimate(
    rule_id: str,
    request: Request,
    access: Access = Depends(require(Perm.EVAL_RULES)),
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
):
    """How many existing traces match in the range, and a rough cost per judged item."""
    try:
        rule = svc.get_rule(db, access.project.id, rule_id)
    except svc.RuleNotFound as exc:
        raise HTTPException(404, "Not found.") from exc
    version = tpl.get_version(db, rule.template_id, rule.template_version)
    model = svc.effective_model(db, access.project.id, rule.connection_id, rule.model, version)
    estimate = svc.per_call_estimate(container.models, access.project.id, version, model.model)
    status = spend.status(db, access.project.id, container.settings)
    matches = None
    if rule.target == "trace":
        snapshot = rules_mod.RuleSnapshot(
            rule.id,
            access.project.id,
            rule.target,
            rule.target_name,
            rule.target_type,
            rule.filter or {},
            rule.sampling_pct,
            0,
            "",
        )
        query = backfill_mod.rule_query(
            snapshot,
            svc.parse_utc(request.query_params.get("from", "")),
            svc.parse_utc(request.query_params.get("to", ""), end_of_day=True),
            None,
        )
        matches = container.trace_store.trace_stats(access.project.id, query).count
    return JSONResponse(
        {
            "matches": matches,
            "limit": container.settings.eval_backfill_max,
            "per_call": estimate.cost,
            "priced": estimate.priced,
            "sampling_pct": rule.sampling_pct,
            "left": max(0.0, status.cap - status.spent - status.reserved),
        }
    )
