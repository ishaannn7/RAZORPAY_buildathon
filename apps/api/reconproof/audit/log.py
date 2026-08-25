"""Append-only audit log.

Every state change that a reviewer or auditor could question goes through
:func:`record`. The sequence number is allocated per batch so a replay can be
ordered deterministically even when two events share a timestamp.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reconproof.db.models import AuditEvent
from reconproof.domain.entities import Actor, AuditAction


def next_sequence(session: Session, batch_id: str | None) -> int:
    current = session.execute(
        select(func.max(AuditEvent.sequence)).where(AuditEvent.batch_id == batch_id)
    ).scalar_one_or_none()
    return (current or 0) + 1


def record(
    session: Session,
    *,
    action: AuditAction,
    actor: Actor,
    batch_id: str | None = None,
    actor_detail: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    agent_run_id: str | None = None,
    model_version_id: str | None = None,
    policy_version_id: str | None = None,
    input_sha256: str | None = None,
    previous_state: dict[str, Any] | None = None,
    resulting_state: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    message: str | None = None,
) -> AuditEvent:
    """Append one audit event. Never updates an existing row."""
    event = AuditEvent(
        batch_id=batch_id,
        sequence=next_sequence(session, batch_id),
        action=action,
        actor=actor,
        actor_detail=actor_detail,
        subject_type=subject_type,
        subject_id=subject_id,
        agent_run_id=agent_run_id,
        model_version_id=model_version_id,
        policy_version_id=policy_version_id,
        input_sha256=input_sha256,
        previous_state=previous_state,
        resulting_state=resulting_state,
        detail=detail or {},
        message=message,
    )
    session.add(event)
    session.flush()
    return event


def canonical_hash(payload: Any) -> str:
    """Stable SHA-256 over a JSON-serializable payload.

    Keys are sorted so the same logical input hashes identically across runs,
    which is what lets an audit entry prove *which* input produced a decision.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
