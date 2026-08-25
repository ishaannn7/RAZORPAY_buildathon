"""HTTP-level gates: promotion cannot skip a human, and ingest must not invent money.

These tests go through FastAPI rather than calling the service layer, because the
claim is about the API a reviewer actually uses. A helper that bypasses the
route would let a bug in the wiring pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.config import Settings
from reconproof.db.models import (
    HumanResolution,
    ModelEvaluation,
    ModelVersion,
    PolicyVersion,
    ReconciliationException,
)
from reconproof.domain.entities import ExceptionStatus, SourceKind
from reconproof.learning.training import materialize_dataset
from reconproof.main import create_app
from reconproof.pipeline import ReconciliationPipeline
from reconproof.policy.engine import PolicyEngine
from reconproof.synthetic.generator import DatasetSpec, write_dataset


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    """Build an app after the settings fixture has pointed the engine at tmp SQLite."""
    return TestClient(create_app())


def _challenger(
    db: Session, *, precision_lower_bound: float, coverage: float = 0.8
) -> ModelVersion:
    version = ModelVersion(name="challenger-logistic", kind="match_scorer", stage="challenger")
    db.add(version)
    db.flush()
    db.add(
        ModelEvaluation(
            model_version_id=version.id,
            dataset_name="holdout",
            split="test",
            metrics={
                "precision_lower_bound": precision_lower_bound,
                "coverage": coverage,
                "precision": precision_lower_bound,
            },
        )
    )
    db.commit()
    db.refresh(version)
    return version


class TestModelPromotionGates:
    def test_evaluate_then_named_human_can_promote(self, client: TestClient, db: Session) -> None:
        version = _challenger(db, precision_lower_bound=0.995)
        listed = client.get("/api/models")
        assert listed.status_code == 200
        row = next(item for item in listed.json() if item["id"] == version.id)
        assert row["stage"] == "challenger"

        evaluated = client.post(f"/api/models/{version.id}/evaluate")
        assert evaluated.status_code == 200, evaluated.text
        body = evaluated.json()
        assert body["passed_gates"] is True
        assert body["requires_human_approval"] is True

        blocked = client.post(f"/api/models/{version.id}/promote")
        assert blocked.status_code == 422  # approved_by is required

        promoted = client.post(
            f"/api/models/{version.id}/promote",
            params={"approved_by": "finance.controller"},
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["stage"] == "production"
        assert promoted.json()["promoted_by"] == "finance.controller"

    def test_weak_bound_cannot_be_promoted(self, client: TestClient, db: Session) -> None:
        version = _challenger(db, precision_lower_bound=0.90)
        evaluated = client.post(f"/api/models/{version.id}/evaluate")
        assert evaluated.status_code == 200, evaluated.text
        assert evaluated.json()["passed_gates"] is False
        assert evaluated.json()["failures"]

        refused = client.post(
            f"/api/models/{version.id}/promote",
            params={"approved_by": "finance.controller"},
        )
        assert refused.status_code == 409
        db.refresh(version)
        assert version.stage == "challenger"


class TestBatchIngestApi:
    def test_create_and_upload_source(self, client: TestClient, tmp_path: Path) -> None:
        created = client.post(
            "/api/batches",
            params={"name": "api-ingest", "currency": "INR"},
        )
        assert created.status_code == 201, created.text
        batch_id = created.json()["id"]
        assert created.json()["status"] == "draft"

        directory = tmp_path / "src"
        write_dataset(DatasetSpec(name="api", seed=11, n_orders=40), directory)
        path = directory / f"{SourceKind.RAZORPAY_PAYMENTS.value}.csv"
        uploaded = client.post(
            f"/api/batches/{batch_id}/sources",
            params={"source_kind": SourceKind.RAZORPAY_PAYMENTS.value},
            files={"file": (path.name, path.read_bytes(), "text/csv")},
        )
        assert uploaded.status_code == 201, uploaded.text
        payload = uploaded.json()
        assert payload["accepted_rows"] > 0
        assert payload["source_kind"] == SourceKind.RAZORPAY_PAYMENTS.value

        detail = client.get(f"/api/batches/{batch_id}")
        assert detail.status_code == 200
        assert detail.json()["record_count"] == payload["accepted_rows"]
        assert len(detail.json()["sources"]) == 1

    def test_unknown_suffix_is_rejected(self, client: TestClient) -> None:
        created = client.post("/api/batches", params={"name": "bad-file"})
        batch_id = created.json()["id"]
        refused = client.post(
            f"/api/batches/{batch_id}/sources",
            files={"file": ("ledger.exe", b"not a csv", "application/octet-stream")},
        )
        assert refused.status_code == 415


class TestExceptionResolutionGates:
    """A second reviewer must not be able to silently overwrite the first.

    Two people resolving the same exception is routine in practice — a
    reviewer picks it up, someone else gets to it a minute later before
    seeing the queue refresh. The first decision has to win, visibly, rather
    than the second one clobbering it.
    """

    def _batch_with_open_exception(
        self, db: Session, settings: Settings, tmp_path: Path
    ) -> tuple[str, str]:
        batch, _truth = materialize_dataset(
            db, DatasetSpec(name="resolve-gate", seed=909, n_orders=300), tmp_path / "resolve-gate"
        )
        ReconciliationPipeline(db, settings=settings).run(batch)
        db.commit()
        exception = (
            db.execute(
                select(ReconciliationException)
                .where(
                    ReconciliationException.batch_id == batch.id,
                    ReconciliationException.status == ExceptionStatus.OPEN,
                )
                .order_by(ReconciliationException.amount_subunits.desc())
            )
            .scalars()
            .first()
        )
        assert exception is not None, "a realistic batch should leave at least one open exception"
        return batch.id, exception.id

    def test_second_resolution_is_refused_once_the_first_is_recorded(
        self, client: TestClient, db: Session, settings: Settings, tmp_path: Path
    ) -> None:
        _batch_id, exception_id = self._batch_with_open_exception(db, settings, tmp_path)

        first = client.post(
            f"/api/exceptions/{exception_id}/resolve",
            json={
                "reviewer": "finance.operator.one",
                "action": "write_off",
                "notes": "first reviewer",
            },
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "written_off"

        second = client.post(
            f"/api/exceptions/{exception_id}/resolve",
            json={
                "reviewer": "finance.operator.two",
                "action": "write_off",
                "notes": "second reviewer, racing the first",
            },
        )
        assert second.status_code == 409

        # The first decision stands: exactly one resolution is on record, and
        # it is the first reviewer's.
        resolutions = list(
            db.execute(
                select(HumanResolution).where(HumanResolution.exception_id == exception_id)
            ).scalars()
        )
        assert len(resolutions) == 1
        assert resolutions[0].reviewer == "finance.operator.one"

        exception = db.get(ReconciliationException, exception_id)
        assert exception is not None
        assert exception.status is ExceptionStatus.WRITTEN_OFF


class TestRuntimeConfigAndPolicyHistory:
    def test_config_exposes_the_agent_tool_allowlist_and_review_rules(
        self, client: TestClient
    ) -> None:
        """A reviewer checking what the agent may do needs this on the same
        page as the automation threshold, not buried in a source file."""
        response = client.get("/api/config")
        assert response.status_code == 200
        policy = response.json()["policy"]
        assert "allowed_tools" in policy["agent"]
        assert "execute_sql" not in policy["agent"]["allowed_tools"]
        assert "force_on_advisory_failure" in policy["review"]

    def test_policies_endpoint_is_empty_before_any_version_is_persisted(
        self, client: TestClient
    ) -> None:
        response = client.get("/api/policies")
        assert response.status_code == 200
        assert response.json() == []

    def test_policies_endpoint_returns_a_persisted_version_with_its_full_document(
        self, client: TestClient, db: Session
    ) -> None:
        policy = PolicyEngine.default()
        db.add(
            PolicyVersion(
                name=policy.name,
                version=policy.version,
                document=policy.document,
                document_sha256=policy.digest(),
                active=True,
                notes="test fixture",
            )
        )
        db.commit()

        response = client.get("/api/policies")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["active"] is True
        assert body[0]["document"]["agent"]["allowed_tools"]
