"""CSV and JSON report exports.

Exercised against a real reconciled batch (not hand-built rows), because the
claim that matters is what the route actually serializes from the database,
including the same formula-injection guard the module docstring promises for
spreadsheet consumers — and, since these routes now also serve `format=json`,
that JSON output carries the same data without corrupting it with a guard
that only makes sense for a spreadsheet cell.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from reconproof.config import Settings
from reconproof.learning.training import materialize_dataset
from reconproof.main import create_app
from reconproof.pipeline import ReconciliationPipeline
from reconproof.synthetic.generator import DatasetSpec


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    return TestClient(create_app())


@pytest.fixture()
def reconciled_batch_id(db: Session, settings: Settings, tmp_path: Path) -> str:
    batch, _truth = materialize_dataset(
        db, DatasetSpec(name="exports", seed=4242, n_orders=150), tmp_path / "exports"
    )
    ReconciliationPipeline(db, settings=settings).run(batch)
    db.commit()
    return batch.id


class TestExceptionsExport:
    def test_csv_is_the_default_and_lists_every_open_exception(
        self, client: TestClient, reconciled_batch_id: str, db: Session
    ) -> None:
        response = client.get(f"/api/batches/{reconciled_batch_id}/export/exceptions")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        rows = response.text.strip().splitlines()
        assert rows[0].startswith("exception_id,category,status,amount")
        # One header row plus one row per exception the batch actually produced.
        assert len(rows) - 1 > 0

    def test_json_carries_the_same_rows_as_csv(
        self, client: TestClient, reconciled_batch_id: str
    ) -> None:
        csv_response = client.get(f"/api/batches/{reconciled_batch_id}/export/exceptions")
        json_response = client.get(
            f"/api/batches/{reconciled_batch_id}/export/exceptions", params={"format": "json"}
        )
        assert json_response.status_code == 200
        assert json_response.headers["content-type"].startswith("application/json")
        payload = json.loads(json_response.text)
        assert isinstance(payload, list)
        csv_row_count = len(csv_response.text.strip().splitlines()) - 1
        assert len(payload) == csv_row_count
        if payload:
            assert set(payload[0].keys()) == {
                "exception_id",
                "category",
                "status",
                "amount",
                "currency",
                "subject_kind",
                "subject_reference",
                "subject_source",
                "occurred_at",
                "best_score",
                "best_risk",
                "blocking_checks",
                "summary",
                "explanation",
            }

    def test_unknown_format_is_rejected(self, client: TestClient, reconciled_batch_id: str) -> None:
        response = client.get(
            f"/api/batches/{reconciled_batch_id}/export/exceptions", params={"format": "xml"}
        )
        assert response.status_code == 422

    def test_unknown_batch_is_404(self, client: TestClient) -> None:
        response = client.get("/api/batches/does-not-exist/export/exceptions")
        assert response.status_code == 404


class TestMatchesExport:
    def test_csv_and_json_agree_on_row_count(
        self, client: TestClient, reconciled_batch_id: str
    ) -> None:
        csv_response = client.get(f"/api/batches/{reconciled_batch_id}/export/matches")
        json_response = client.get(
            f"/api/batches/{reconciled_batch_id}/export/matches", params={"format": "json"}
        )
        assert csv_response.status_code == json_response.status_code == 200
        csv_rows = len(csv_response.text.strip().splitlines()) - 1
        payload = json.loads(json_response.text)
        assert len(payload) == csv_rows
        assert csv_rows > 0

    def test_decision_filter_narrows_both_formats_identically(
        self, client: TestClient, reconciled_batch_id: str
    ) -> None:
        csv_response = client.get(
            f"/api/batches/{reconciled_batch_id}/export/matches",
            params={"decision": "auto_accepted"},
        )
        json_response = client.get(
            f"/api/batches/{reconciled_batch_id}/export/matches",
            params={"decision": "auto_accepted", "format": "json"},
        )
        csv_rows = len(csv_response.text.strip().splitlines()) - 1
        payload = json.loads(json_response.text)
        assert len(payload) == csv_rows
        assert all(row["decision"] == "auto_accepted" for row in payload)


class TestAuditExport:
    def test_csv_and_json_both_serve_the_full_sequence(
        self, client: TestClient, reconciled_batch_id: str
    ) -> None:
        csv_response = client.get(f"/api/batches/{reconciled_batch_id}/export/audit")
        json_response = client.get(
            f"/api/batches/{reconciled_batch_id}/export/audit", params={"format": "json"}
        )
        assert csv_response.status_code == json_response.status_code == 200
        payload = json.loads(json_response.text)
        csv_rows = len(csv_response.text.strip().splitlines()) - 1
        assert len(payload) == csv_rows
        # Sequence numbers are contiguous starting at 1, per the audit trail's own guarantee.
        sequences = [row["sequence"] for row in payload]
        assert sequences == sorted(sequences)


class TestFormulaInjectionGuard:
    """The guard is a spreadsheet-only concern; JSON must not apply it."""

    def test_csv_neutralizes_a_formula_looking_summary_but_json_does_not(
        self, client: TestClient, db: Session, reconciled_batch_id: str
    ) -> None:
        from sqlalchemy import select

        from reconproof.db.models import ReconciliationException

        exception = (
            db.execute(
                select(ReconciliationException).where(
                    ReconciliationException.batch_id == reconciled_batch_id
                )
            )
            .scalars()
            .first()
        )
        assert exception is not None
        exception.summary = "=SUM(A1:A10)"
        db.commit()

        csv_response = client.get(f"/api/batches/{reconciled_batch_id}/export/exceptions")
        json_response = client.get(
            f"/api/batches/{reconciled_batch_id}/export/exceptions", params={"format": "json"}
        )
        assert "'=SUM(A1:A10)" in csv_response.text
        payload = json.loads(json_response.text)
        matching = [row for row in payload if row["exception_id"] == exception.id]
        assert matching and matching[0]["summary"] == "=SUM(A1:A10)"
