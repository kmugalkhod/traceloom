"""Demo data for `traceloom seed-demo`: a user, org, project and API key pair, idempotently.

The login comes from Settings (TRACELOOM_DEMO_EMAIL / TRACELOOM_DEMO_PASSWORD). The key pair is
derived from the server secret (TRACELOOM_SERVER_SECRET, read via Settings), so
running the command again prints the same keys (a stored secret cannot be recovered from its
hash) and they are unique to each install.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from traceloom.annotation import queues as annotation_queues
from traceloom.auth.api_keys import PUBLIC_PREFIX, SECRET_PREFIX, hash_secret
from traceloom.auth.passwords import hash_password
from traceloom.auth.permissions import Role
from traceloom.config import Settings
from traceloom.datasets import runs as dataset_runs
from traceloom.datasets import service as datasets
from traceloom.db.models import (
    AnnotationQueue,
    ApiKey,
    Membership,
    Organization,
    Project,
    User,
)
from traceloom.db.models_datasets import DatasetItem
from traceloom.prompts.service import Actor as PromptActor
from traceloom.scores import configs as score_configs

DEMO_ORG = "Demo Org"
DEMO_PROJECT = "Demo Project"
DEMO_KEY_NOTE = "seed-demo"
DEMO_CONFIG = "helpfulness"
DEMO_QUEUE = "Demo review queue"
DEMO_DATASET = "demo-support-questions"
DEMO_DATASET_ITEMS = 20
DEMO_RUNS = ("demo-run-baseline", "demo-run-candidate")


@dataclass(frozen=True)
class DemoInfo:
    email: str
    password: str
    org_id: str
    project_id: str
    public_key: str
    secret_key: str
    created: bool


def _derive(settings: Settings, label: str, length: int) -> str:
    digest = hmac.new(settings.secret_key.encode(), f"seed-demo:{label}".encode(), hashlib.sha256)
    return digest.hexdigest()[:length]


def seed_demo(settings: Settings, session_factory: sessionmaker[Session]) -> DemoInfo:
    """Create the demo user, org, project and key if missing. Safe to run repeatedly."""
    email, password = settings.demo_email, settings.demo_password
    public_key = f"{PUBLIC_PREFIX}demo-{_derive(settings, 'public', 12)}"
    secret_key = f"{SECRET_PREFIX}demo-{_derive(settings, 'secret', 32)}"
    created = False
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, name="Demo User", password_hash=hash_password(password))
            db.add(user)
            created = True
        org = db.scalar(
            select(Organization)
            .join(Membership, Membership.org_id == Organization.id)
            .where(Membership.user_id == user.id, Organization.name == DEMO_ORG)
        )
        if org is None:
            org = Organization(name=DEMO_ORG)
            db.add(org)
            db.flush()
            db.add(Membership(user_id=user.id, org_id=org.id, role=Role.OWNER.label))
            created = True
        project = db.scalar(
            select(Project).where(Project.org_id == org.id, Project.name == DEMO_PROJECT)
        )
        if project is None:
            project = Project(org_id=org.id, name=DEMO_PROJECT)
            db.add(project)
            db.flush()
            created = True
        key = db.scalar(select(ApiKey).where(ApiKey.public_key == public_key))
        if key is None:
            db.add(
                ApiKey(
                    project_id=project.id,
                    public_key=public_key,
                    secret_hash=hash_secret(secret_key, settings.secret_key),
                    note=DEMO_KEY_NOTE,
                )
            )
            created = True
        elif key.revoked_at is not None:
            key.revoked_at = None  # the demo key is meant to be usable
        _seed_evaluation(db, project, user, org_role=Role.OWNER)
        db.commit()
        return DemoInfo(email, password, org.id, project.id, public_key, secret_key, created)


def _seed_evaluation(db: Session, project: Project, user: User, org_role: Role) -> None:
    """A score config, a queue, a 20-item dataset and two runs; each is created only once."""
    config = next(
        (c for c in score_configs.list_configs(db, project.id) if c.name == DEMO_CONFIG), None
    )
    if config is None:
        config = score_configs.create_config(
            db,
            project.id,
            created_by=user.id,
            name=DEMO_CONFIG,
            data_type="NUMERIC",
            min_value=1,
            max_value=5,
            description="How helpful the answer was, from 1 to 5.",
        )
    if (
        db.scalar(
            select(AnnotationQueue.id).where(
                AnnotationQueue.project_id == project.id, AnnotationQueue.name == DEMO_QUEUE
            )
        )
        is None
    ):
        annotation_queues.create_queue(
            db,
            project,
            PromptActor.for_user(user, org_role),
            DEMO_QUEUE,
            "Traces waiting for a human score.",
            [config.id],
        )
    actor = datasets.Actor.for_user(user, org_role)
    dataset = datasets.find_dataset(db, project, DEMO_DATASET)
    if dataset is None:
        dataset = datasets.create_dataset(
            db,
            project,
            actor,
            name=DEMO_DATASET,
            description="Twenty support questions with expected answers.",
        )
        for n in range(1, DEMO_DATASET_ITEMS + 1):
            datasets.create_item(
                db,
                project,
                actor,
                dataset,
                input={"question": f"Demo question {n}"},
                expected_output={"answer": f"Demo answer {n}"},
            )
    items = db.scalars(select(DatasetItem).where(DatasetItem.dataset_id == dataset.id)).all()
    for run_name in DEMO_RUNS:
        if dataset_runs.find_run(db, project, dataset, run_name):
            continue
        run = dataset_runs.create_run(db, project, actor, dataset, name=run_name)
        for item in items:
            dataset_runs.link_run_item(
                db,
                project,
                actor,
                run,
                item,
                output={"answer": f"{run_name} answer"},
                latency_ms=120.0,
            )
        dataset_runs.set_status(db, project, actor, run, "completed")
