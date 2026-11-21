"""The judge spend ledger and daily cost cap (FR-J6, ADR-0014).

`eval_spend_daily` holds, per project and UTC day, the money already spent and the money reserved
by calls in flight. A call reserves its estimated cost before it runs; the reservation is one
conditional UPDATE, so concurrent workers can never reserve past the cap. After the call the
reservation is settled (replaced by the actual cost) or released (the call never happened).

A reservation remembers the day it was made on, so a call that finishes after UTC midnight still
settles against the day that was charged.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, sessionmaker

from traceloom.config import Settings
from traceloom.cost.pricing import effective_prices
from traceloom.cost.registry import ModelDef
from traceloom.cost.tokenizers import count_tokens
from traceloom.db.models_eval import EvalSpendDaily, ProjectEvalSettings

DEFAULT_MAX_TOKENS = 1024
CHARS_PER_TOKEN = 4  # fallback when the model has no tokenizer
_EPSILON = 1e-9  # float slack so a reservation that exactly fills the cap is accepted


class CapReached(Exception):
    def __init__(self, cap: float) -> None:
        self.cap = cap
        super().__init__(f"daily cost cap of ${cap:g} reached")


@dataclass(frozen=True)
class Estimate:
    cost: float
    priced: bool  # False: the model has no price definition, so the cap cannot protect it


@dataclass(frozen=True)
class Reservation:
    project_id: str
    day: date
    amount: float
    priced: bool = True


@dataclass(frozen=True)
class SpendStatus:
    day: date
    cap: float
    spent: float
    reserved: float

    @property
    def paused(self) -> bool:
        return self.cap <= 0

    @property
    def reached(self) -> bool:
        return self.paused or self.spent + self.reserved >= self.cap - _EPSILON


def utc_day(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(UTC).date()


# --- Estimate ----------------------------------------------------------------------------------


def count_prompt_tokens(definition: ModelDef | None, text: str) -> int:
    counted = count_tokens(definition.tokenizer, text) if definition else None
    return counted if counted is not None else math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_cost(
    definition: ModelDef | None, prompt_tokens: int, max_tokens: int | None = None
) -> Estimate:
    """prompt tokens x input price + max_tokens x output price. A model with no definition, or
    without an input or output price, is unpriced and estimates to 0."""
    if definition is None:
        return Estimate(0.0, False)
    prices = effective_prices(definition, {"input": prompt_tokens})
    if "input" not in prices and "output" not in prices:
        return Estimate(0.0, False)
    out_tokens = max_tokens or DEFAULT_MAX_TOKENS
    cost = prompt_tokens * prices.get("input", 0.0) + out_tokens * prices.get("output", 0.0)
    return Estimate(cost, True)


# --- Cap ---------------------------------------------------------------------------------------


def get_cap(db: Session, project_id: str, settings: Settings) -> float:
    """The project's override, else the env default. 0 means judges are paused."""
    override = db.scalar(
        select(ProjectEvalSettings.daily_cost_cap_usd).where(
            ProjectEvalSettings.project_id == project_id
        )
    )
    return float(settings.eval_daily_cost_cap_usd if override is None else override)


def set_cap(db: Session, project_id: str, cap: float | None) -> None:
    """Set the project's cap (None: back to the env default)."""
    if cap is not None and (not math.isfinite(cap) or cap < 0):
        raise ValueError("the cap must be zero or more")
    row = db.get(ProjectEvalSettings, project_id)
    if row is None:
        db.add(ProjectEvalSettings(project_id=project_id, daily_cost_cap_usd=cap))
    else:
        row.daily_cost_cap_usd = cap
    db.flush()


def status(
    db: Session, project_id: str, settings: Settings, now: datetime | None = None
) -> SpendStatus:
    day = utc_day(now)
    row = db.get(EvalSpendDaily, (project_id, day))
    return SpendStatus(
        day,
        get_cap(db, project_id, settings),
        row.spent_usd if row else 0.0,
        row.reserved_usd if row else 0.0,
    )


# --- Ledger ------------------------------------------------------------------------------------


def _insert_if_missing(db: Session, project_id: str, day: date) -> None:
    dialect = db.get_bind().dialect.name
    insert = (postgresql if dialect == "postgresql" else sqlite).insert
    db.execute(
        insert(EvalSpendDaily)
        .values(project_id=project_id, day=day, spent_usd=0.0, reserved_usd=0.0)
        .on_conflict_do_nothing(index_elements=["project_id", "day"])
    )


def reserve(
    sessions: sessionmaker[Session],
    project_id: str,
    estimate: Estimate,
    cap: float,
    now: datetime | None = None,
) -> Reservation:
    """Hold `estimate.cost` against today's cap, or raise CapReached.

    A cap of 0 rejects everything. An unpriced model estimates to 0 and is allowed while the
    cap is above 0, without touching the ledger."""
    if cap <= 0:
        raise CapReached(cap)
    day = utc_day(now)
    if estimate.cost <= 0:
        return Reservation(project_id, day, 0.0, estimate.priced)
    with sessions.begin() as db:
        _insert_if_missing(db, project_id, day)
        taken = db.execute(
            update(EvalSpendDaily)
            .where(
                EvalSpendDaily.project_id == project_id,
                EvalSpendDaily.day == day,
                EvalSpendDaily.spent_usd + EvalSpendDaily.reserved_usd + estimate.cost
                <= cap + _EPSILON,
            )
            .values(reserved_usd=EvalSpendDaily.reserved_usd + estimate.cost)
        ).rowcount
    if not taken:
        raise CapReached(cap)
    return Reservation(project_id, day, estimate.cost, estimate.priced)


def _drop_reservation(amount: float):
    left = EvalSpendDaily.reserved_usd - amount
    return case((left < 0, 0.0), else_=left)  # never negative, even after a manual reset


# Runs inside the settle/release transaction. True while the job still owns its claimed row (and
# so its reservation); it also zeroes the row's `estimated_cost`. False once the sweeper has reset
# the row, which already released the reservation.
StillHeld = Callable[[Session], bool]


def claim_check(model: Any, row_id: str, *conditions: Any) -> StillHeld:
    """A StillHeld for a job row: `status == running` plus the claim's own `conditions`."""

    def check(db: Session) -> bool:
        return bool(
            db.execute(
                update(model)
                .where(model.id == row_id, model.status == "running", *conditions)
                .values(estimated_cost=0.0)
            ).rowcount
        )

    return check


def settle(
    sessions: sessionmaker[Session],
    reservation: Reservation,
    actual: float | None,
    still_held: StillHeld | None = None,
) -> None:
    """Replace the reservation with what the call really cost, on the day it was reserved.

    An unknown cost (None) is charged at the reservation, never as free. When `still_held` says
    the sweeper already reset the row, only the spend is added: its hold was released then."""
    actual = reservation.amount if actual is None else max(actual, 0.0)
    if reservation.amount <= 0 and actual <= 0:
        return
    with sessions.begin() as db:
        held = still_held(db) if still_held else True
        _insert_if_missing(db, reservation.project_id, reservation.day)
        values: dict[str, Any] = {"spent_usd": EvalSpendDaily.spent_usd + actual}
        if held:
            values["reserved_usd"] = _drop_reservation(reservation.amount)
        db.execute(
            update(EvalSpendDaily)
            .where(
                EvalSpendDaily.project_id == reservation.project_id,
                EvalSpendDaily.day == reservation.day,
            )
            .values(**values)
        )


def release(
    sessions: sessionmaker[Session],
    reservation: Reservation,
    still_held: StillHeld | None = None,
) -> None:
    """Free a reservation whose call never ran (or died). Nothing to do when `still_held` says
    the sweeper already reset the row (and released it)."""
    if reservation.amount <= 0:
        return
    with sessions.begin() as db:
        if still_held and not still_held(db):
            return
        db.execute(
            update(EvalSpendDaily)
            .where(
                EvalSpendDaily.project_id == reservation.project_id,
                EvalSpendDaily.day == reservation.day,
            )
            .values(reserved_usd=_drop_reservation(reservation.amount))
        )
