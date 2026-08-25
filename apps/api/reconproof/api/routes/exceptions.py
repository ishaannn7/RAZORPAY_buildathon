"""Exception queue and human review endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.api.schemas import (
    Amount,
    ExceptionDetail,
    ExceptionSummary,
    ResolutionSummary,
    ResolveRequest,
)
from reconproof.api.serializers import (
    build_counterfactual,
    serialize_candidate,
    serialize_evidence,
    serialize_record,
)
from reconproof.audit.log import record as audit_record
from reconproof.db.models import (
    AccountingCheck,
    AgentRun,
    EvidenceItem,
    HumanResolution,
    MatchCandidate,
    ReconciliationException,
    ReconciliationMatch,
    SourceRecord,
)
from reconproof.db.session import get_db
from reconproof.domain.entities import (
    Actor,
    AuditAction,
    ExceptionCategory,
    ExceptionStatus,
    MatchDecision,
    MatchMethod,
)
from reconproof.learning.store import record_label

router = APIRouter(prefix="/exceptions", tags=["exceptions"])


def _get_exception(db: Session, exception_id: str) -> ReconciliationException:
    item = db.get(ReconciliationException, exception_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"exception {exception_id} not found")
    return item


def _latest_agent_run(db: Session, exception_id: str) -> AgentRun | None:
    return (
        db.execute(
            select(AgentRun)
            .where(AgentRun.exception_id == exception_id)
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _summarize(db: Session, item: ReconciliationException) -> ExceptionSummary:
    subject = db.get(SourceRecord, item.subject_record_id)
    if subject is None:  # pragma: no cover - foreign key guarantees this
        raise HTTPException(status_code=500, detail="exception subject record is missing")
    run = _latest_agent_run(db, item.id)
    return ExceptionSummary(
        id=item.id,
        batch_id=item.batch_id,
        category=item.category.value,
        status=item.status.value,
        amount=Amount.of(item.amount_subunits, item.currency),
        summary=item.summary,
        best_score=item.best_score,
        best_risk=item.best_risk,
        blocking_invariants=item.blocking_invariants or [],
        explanation=item.explanation,
        explanation_provider=item.explanation_provider,
        created_at=item.created_at,
        subject=serialize_record(subject),
        agent_run_id=run.id if run else None,
        agent_outcome=run.outcome.value if run and run.outcome else None,
    )


@router.get("", response_model=list[ExceptionSummary])
def list_exceptions(
    db: Annotated[Session, Depends(get_db)],
    batch_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ExceptionSummary]:
    statement = select(ReconciliationException)
    if batch_id:
        statement = statement.where(ReconciliationException.batch_id == batch_id)
    if status:
        try:
            statement = statement.where(ReconciliationException.status == ExceptionStatus(status))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"unknown status {status!r}") from exc
    if category:
        try:
            statement = statement.where(
                ReconciliationException.category == ExceptionCategory(category)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"unknown category {category!r}") from exc
    # Largest unresolved value first: that is the order a finance team works in.
    statement = (
        statement.order_by(ReconciliationException.amount_subunits.desc())
        .limit(limit)
        .offset(offset)
    )
    return [_summarize(db, item) for item in db.execute(statement).scalars()]


@router.get("/{exception_id}", response_model=ExceptionDetail)
def get_exception(exception_id: str, db: Annotated[Session, Depends(get_db)]) -> ExceptionDetail:
    item = _get_exception(db, exception_id)
    evidence = list(
        db.execute(select(EvidenceItem).where(EvidenceItem.exception_id == item.id)).scalars()
    )

    # Candidates reachable from this exception's subject, so a reviewer sees the
    # competing options rather than only the one the pipeline preferred.
    candidates = list(
        db.execute(
            select(MatchCandidate)
            .where(
                MatchCandidate.batch_id == item.batch_id,
                (MatchCandidate.left_record_id == item.subject_record_id)
                | (MatchCandidate.right_record_id == item.subject_record_id),
            )
            .order_by(MatchCandidate.score.desc().nullslast())
            .limit(25)
        ).scalars()
    )
    record_ids = {item.subject_record_id}
    for candidate in candidates:
        record_ids.add(candidate.left_record_id)
        record_ids.add(candidate.right_record_id)
    records = {
        record.id: record
        for record in db.execute(
            select(SourceRecord).where(SourceRecord.id.in_(record_ids))
        ).scalars()
    }
    checks_by_candidate: dict[str, list[AccountingCheck]] = {}
    evidence_by_candidate: dict[str, list[EvidenceItem]] = {}
    if candidates:
        candidate_ids = [candidate.id for candidate in candidates]
        for check in db.execute(
            select(AccountingCheck).where(AccountingCheck.candidate_id.in_(candidate_ids))
        ).scalars():
            checks_by_candidate.setdefault(check.candidate_id or "", []).append(check)
        for evidence_item in db.execute(
            select(EvidenceItem).where(EvidenceItem.candidate_id.in_(candidate_ids))
        ).scalars():
            evidence_by_candidate.setdefault(evidence_item.candidate_id or "", []).append(
                evidence_item
            )

    serialized = [
        serialize_candidate(
            candidate,
            records[candidate.left_record_id],
            records[candidate.right_record_id],
            evidence_by_candidate.get(candidate.id, []),
            checks_by_candidate.get(candidate.id, []),
        )
        for candidate in candidates
        if candidate.left_record_id in records and candidate.right_record_id in records
    ]

    best_features = serialized[0].features if serialized else {}
    counterfactual = build_counterfactual(best_features, item.blocking_invariants or [])

    resolution = (
        db.execute(
            select(HumanResolution)
            .where(HumanResolution.exception_id == item.id)
            .order_by(HumanResolution.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )

    return ExceptionDetail(
        **_summarize(db, item).model_dump(),
        candidates=serialized,
        evidence=[serialize_evidence(entry) for entry in evidence],
        counterfactual=counterfactual,
        resolution=ResolutionSummary.model_validate(resolution) if resolution else None,
    )


@router.post("/{exception_id}/resolve", response_model=ExceptionDetail)
def resolve_exception(
    exception_id: str,
    payload: ResolveRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ExceptionDetail:
    """Record a human decision and, where one was chosen, post the match.

    Only this path can post a match that the automatic layer declined. An agent
    recommendation is never sufficient on its own: it becomes a proposal that a
    reviewer either approves here or overrides.
    """
    item = _get_exception(db, exception_id)
    if item.status in (ExceptionStatus.RESOLVED, ExceptionStatus.WRITTEN_OFF):
        raise HTTPException(status_code=409, detail=f"exception is already {item.status.value}")

    run = _latest_agent_run(db, item.id)
    recommended_candidate = None
    if run and run.recommendation:
        recommended_candidate = run.recommendation.get("candidate_id")

    previous_status = item.status.value
    chosen_candidate: MatchCandidate | None = None

    if payload.action in {"approve", "choose"}:
        candidate_id = payload.candidate_id
        if payload.action == "approve":
            candidate_id = candidate_id or recommended_candidate
            if not candidate_id:
                raise HTTPException(
                    status_code=400,
                    detail="approve requires a candidate_id, or an agent recommendation to approve",
                )
        if not candidate_id:
            raise HTTPException(status_code=400, detail="choose requires a candidate_id")
        chosen_candidate = db.get(MatchCandidate, candidate_id)
        if chosen_candidate is None or chosen_candidate.batch_id != item.batch_id:
            raise HTTPException(
                status_code=400, detail=f"candidate {candidate_id} does not belong to this batch"
            )

        left = db.get(SourceRecord, chosen_candidate.left_record_id)
        right = db.get(SourceRecord, chosen_candidate.right_record_id)
        if left is None or right is None:  # pragma: no cover - foreign keys
            raise HTTPException(status_code=500, detail="candidate records are missing")

        # A human decision is recorded as its own match with the human method,
        # so an auditor can always tell an approved link from an automatic one.
        db.add(
            ReconciliationMatch(
                batch_id=item.batch_id,
                candidate_id=chosen_candidate.id,
                left_record_id=left.id,
                right_record_id=right.id,
                relation=chosen_candidate.relation,
                decision=MatchDecision.AUTO_ACCEPTED,
                method=MatchMethod.HUMAN_RESOLUTION,
                score=chosen_candidate.score,
                risk=chosen_candidate.risk,
                allocated_subunits=(
                    -abs(left.amount_subunits)
                    if chosen_candidate.relation.value.startswith(("refund", "fee"))
                    else left.amount_subunits
                ),
                currency=left.currency,
                rationale=f"Approved by {payload.reviewer}",
            )
        )
        item.status = ExceptionStatus.RESOLVED
    elif payload.action == "mark_unresolved":
        item.status = ExceptionStatus.OPEN
    else:  # write_off
        item.status = ExceptionStatus.WRITTEN_OFF

    if payload.reclassify:
        try:
            item.category = ExceptionCategory(payload.reclassify)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"unknown category {payload.reclassify!r}"
            ) from exc

    overrode = bool(
        chosen_candidate and recommended_candidate and chosen_candidate.id != recommended_candidate
    )
    resolution = HumanResolution(
        exception_id=item.id,
        batch_id=item.batch_id,
        reviewer=payload.reviewer,
        action=payload.action,
        chosen_candidate_id=chosen_candidate.id if chosen_candidate else None,
        chosen_record_id=chosen_candidate.right_record_id if chosen_candidate else None,
        reclassified_category=item.category if payload.reclassify else None,
        notes=payload.notes,
        overrode_agent=overrode,
        agent_run_id=run.id if run else None,
    )
    db.add(resolution)
    db.flush()

    # The decision becomes a labelled example for future model training.
    if chosen_candidate is not None:
        record_label(db, exception=item, candidate=chosen_candidate, resolution=resolution)

    audit_record(
        db,
        action=(
            AuditAction.HUMAN_OVERRODE
            if overrode
            else AuditAction.HUMAN_APPROVED
            if chosen_candidate
            else AuditAction.HUMAN_MARKED_UNRESOLVED
        ),
        actor=Actor.HUMAN,
        actor_detail=payload.reviewer,
        batch_id=item.batch_id,
        subject_type="exception",
        subject_id=item.id,
        agent_run_id=run.id if run else None,
        previous_state={"status": previous_status},
        resulting_state={"status": item.status.value},
        detail={
            "action": payload.action,
            "candidate_id": chosen_candidate.id if chosen_candidate else None,
            "overrode_agent": overrode,
            "notes": payload.notes,
        },
        message=f"{payload.reviewer} {payload.action} exception {item.id}",
    )
    db.commit()
    return get_exception(exception_id, db)
