"""HTTP-level gates: promotion cannot skip a human, and ingest must not invent money.

These tests go through FastAPI rather than calling the service layer, because the
claim is about the API a reviewer actually uses. A helper that bypasses the
route would let a bug in the wiring pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from reconproof.config import Settings
from reconproof.db.models import ModelEvaluation, ModelVersion
from reconproof.domain.entities import SourceKind
from reconproof.main import create_app
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
