"""Agent investigation endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.agent.investigation import Investigator
from reconproof.agent.providers.registry import describe_provider
from reconproof.api.schemas import (
    AgentRunDetail,
    AgentRunSummary,
    AgentStepSummary,
    BriefingSummary,
    ToolCallSummary,
)
from reconproof.config import Settings, get_settings
from reconproof.db.models import (
    AgentRun,
    AgentStep,
    BatchBriefing,
    ReconciliationBatch,
    ReconciliationException,
    ToolCall,
)
from reconproof.db.session import get_db
from reconproof.domain.entities import ExceptionStatus

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/provider")
def provider_status(config: Annotated[Settings, Depends(get_settings)]) -> dict[str, object]:
    return describe_provider(config)


@router.post("/investigate/{exception_id}", response_model=AgentRunDetail)
def investigate(
    exception_id: str,
    db: Annotated[Session, Depends(get_db)],
    config: Annotated[Settings, Depends(get_settings)],
) -> AgentRunDetail:
    """Run one bounded investigation.

    The result is always a proposal or an abstention. Nothing here can post a
    match: approving a recommendation is a separate, human-only action on the
    exception resolve endpoint.
    """
    exception = db.get(ReconciliationException, exception_id)
    if exception is None:
        raise HTTPException(status_code=404, detail=f"exception {exception_id} not found")
    if exception.status is ExceptionStatus.RESOLVED:
        raise HTTPException(status_code=409, detail="exception is already resolved")

    investigator = Investigator(db, settings=config)
    outcome = investigator.investigate(exception)
    db.commit()
    return get_run(outcome.run_id, db)


@router.get("/runs", response_model=list[AgentRunSummary])
def list_runs(
    db: Annotated[Session, Depends(get_db)],
    batch_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AgentRunSummary]:
    statement = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit)
    if batch_id:
        statement = statement.where(AgentRun.batch_id == batch_id)
    return [
        AgentRunSummary(
            id=run.id,
            batch_id=run.batch_id,
            exception_id=run.exception_id,
            kind=run.kind,
            phase=run.phase.value,
            outcome=run.outcome.value if run.outcome else None,
            provider=run.provider,
            model_name=run.model_name,
            iterations=run.iterations,
            tool_calls=run.tool_calls,
            denied_tool_calls=run.denied_tool_calls,
            invalid_outputs=run.invalid_outputs,
            duration_ms=run.duration_ms,
            recommendation=run.recommendation,
            cited_evidence_ids=run.cited_evidence_ids or [],
            rejected_hypotheses=run.rejected_hypotheses or [],
            abstain_reason=run.abstain_reason,
            created_at=run.created_at,
        )
        for run in db.execute(statement).scalars()
    ]


@router.get("/runs/{run_id}", response_model=AgentRunDetail)
def get_run(run_id: str, db: Annotated[Session, Depends(get_db)]) -> AgentRunDetail:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"agent run {run_id} not found")
    steps = list(
        db.execute(
            select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.sequence)
        ).scalars()
    )
    tools = list(
        db.execute(
            select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.sequence)
        ).scalars()
    )
    return AgentRunDetail(
        id=run.id,
        batch_id=run.batch_id,
        exception_id=run.exception_id,
        kind=run.kind,
        phase=run.phase.value,
        outcome=run.outcome.value if run.outcome else None,
        provider=run.provider,
        model_name=run.model_name,
        iterations=run.iterations,
        tool_calls=run.tool_calls,
        denied_tool_calls=run.denied_tool_calls,
        invalid_outputs=run.invalid_outputs,
        duration_ms=run.duration_ms,
        recommendation=run.recommendation,
        cited_evidence_ids=run.cited_evidence_ids or [],
        rejected_hypotheses=run.rejected_hypotheses or [],
        abstain_reason=run.abstain_reason,
        created_at=run.created_at,
        steps=[
            AgentStepSummary(
                id=step.id,
                sequence=step.sequence,
                phase=step.phase.value,
                thought=step.thought,
                output=step.output,
                duration_ms=step.duration_ms,
            )
            for step in steps
        ],
        tools=[ToolCallSummary.model_validate(call) for call in tools],
    )


@router.post("/briefing/{batch_id}", response_model=BriefingSummary)
def create_briefing(
    batch_id: str,
    db: Annotated[Session, Depends(get_db)],
    config: Annotated[Settings, Depends(get_settings)],
) -> BriefingSummary:
    """Produce a controller-facing briefing for a completed batch."""
    from reconproof.monitoring.briefing import build_briefing

    batch = db.get(ReconciliationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"batch {batch_id} not found")
    briefing = build_briefing(db, batch, settings=config)
    db.commit()
    return BriefingSummary.model_validate(briefing)


@router.get("/briefing/{batch_id}", response_model=list[BriefingSummary])
def list_briefings(batch_id: str, db: Annotated[Session, Depends(get_db)]) -> list[BriefingSummary]:
    briefings = list(
        db.execute(
            select(BatchBriefing)
            .where(BatchBriefing.batch_id == batch_id)
            .order_by(BatchBriefing.created_at.desc())
        ).scalars()
    )
    return [BriefingSummary.model_validate(briefing) for briefing in briefings]
