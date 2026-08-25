"""Policy version history.

`GET /api/config` answers "what governs automation right now" with the fields
a reviewer needs on every page. This router answers a narrower, less frequent
question — "what has governed it, ever, and what did each version actually
say" — for the settings and audit surfaces where a full document, including
the agent's tool allowlist and review rules, is the point rather than noise.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.api.schemas import PolicyVersionSummary
from reconproof.db.models import PolicyVersion
from reconproof.db.session import get_db

router = APIRouter(prefix="/policies", tags=["policies"])


@router.get("", response_model=list[PolicyVersionSummary])
def list_policies(db: Annotated[Session, Depends(get_db)]) -> list[PolicyVersion]:
    return list(
        db.execute(select(PolicyVersion).order_by(PolicyVersion.created_at.desc())).scalars()
    )
