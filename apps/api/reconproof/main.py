"""FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from reconproof.api.routes import (
    agent,
    batches,
    exceptions,
    exports,
    models,
    monitoring,
    policies,
)
from reconproof.api.schemas import HealthResponse
from reconproof.config import Settings, get_settings
from reconproof.db.models import ModelVersion
from reconproof.db.session import create_all, get_db
from reconproof.runtime import load_active_scorer

logger = structlog.get_logger(__name__)

DESCRIPTION = """
Uncertainty-aware financial reconciliation.

Automatic decisions are gated on a proven risk bound, not a raw model
probability, and every accepted link satisfies the accounting invariants. Work
the pipeline cannot prove is routed to the exception queue with the evidence
attached, rather than resolved by a guess.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    create_all()
    scorer = load_active_scorer(settings)
    logger.info(
        "reconproof.started",
        database=settings.resolved_database_url.split("://", 1)[0],
        scorer="calibrated" if scorer else "deterministic-only",
    )
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ReconProof",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        # Explicit origins rather than a wildcard: the API is read/write and
        # a wildcard would let any page drive a reviewer's session.
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(batches.router, prefix="/api")
    app.include_router(exports.router, prefix="/api")
    app.include_router(exceptions.router, prefix="/api")
    app.include_router(agent.router, prefix="/api")
    app.include_router(monitoring.router, prefix="/api")
    app.include_router(models.router, prefix="/api")
    app.include_router(policies.router, prefix="/api")

    @app.get("/api/health", response_model=HealthResponse, tags=["meta"])
    def health(
        db: Annotated[Session, Depends(get_db)],
        config: Annotated[Settings, Depends(get_settings)],
    ) -> HealthResponse:
        try:
            db.execute(text("SELECT 1"))
            database = "ok"
        except Exception as exc:
            database = f"error: {exc}"

        from reconproof.agent.providers.registry import describe_provider

        provider = describe_provider(config)
        stage = (
            db.execute(
                select(ModelVersion.stage).where(ModelVersion.stage == "production").limit(1)
            )
            .scalars()
            .first()
        )
        return HealthResponse(
            status="ok" if database == "ok" else "degraded",
            database=database,
            llm_provider=provider["name"],
            llm_available=provider["available"],
            model_stage=stage,
            semantic_matching=config.enable_semantic_matching,
            version="0.1.0",
        )

    @app.get("/api/config", tags=["meta"])
    def runtime_config(
        config: Annotated[Settings, Depends(get_settings)],
    ) -> dict[str, Any]:
        """Expose the thresholds the UI needs to explain a decision."""
        from reconproof.agent.providers.registry import describe_provider
        from reconproof.policy.engine import PolicyEngine
        from reconproof.runtime import scorer_summary

        policy = PolicyEngine.default()
        return {
            "target_precision": config.target_precision,
            "risk_budget": config.risk_budget,
            "policy": {
                "name": policy.name,
                "version": policy.version,
                "digest": policy.digest()[:16],
                "max_risk": policy.document["automation"]["max_risk"],
                "high_value_review_subunits": policy.document["automation"][
                    "high_value_review_subunits"
                ],
                # The full agent and review sub-documents, not just the
                # automation threshold: a reviewer checking what the agent is
                # permitted to do needs the tool allowlist and iteration
                # limits on the same page as the acceptance threshold.
                "agent": policy.document["agent"],
                "review": policy.document["review"],
                "drift": policy.document["drift"],
            },
            "scorer": scorer_summary(config),
            "llm": describe_provider(config),
            "database": config.resolved_database_url.split("://", 1)[0],
        }

    return app


app = create_app()


def main() -> None:
    """Entry point for ``uvicorn`` via ``python -m reconproof.main``."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "reconproof.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
