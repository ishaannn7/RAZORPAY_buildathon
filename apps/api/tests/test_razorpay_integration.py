"""Razorpay adapter: signature verification, JSON-to-CSV translation, and the
two live routes (pull sync, push webhook).

The translation tests prove exactness end to end through `ingest_file` rather
than asserting on the CSV bytes directly, because the claim that matters is
what lands in the database, not what the intermediate format looks like.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.config import Settings
from reconproof.db.models import ReconciliationBatch, SourceRecord
from reconproof.domain.entities import BatchStatus, RecordKind, SourceKind
from reconproof.ingest.loader import ingest_file
from reconproof.integrations.razorpay import (
    RazorpayAPIError,
    RazorpayClient,
    payments_to_csv,
    refunds_to_csv,
    verify_webhook_signature,
    webhook_event_to_csv,
)
from reconproof.main import create_app


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    return TestClient(create_app())


@pytest.fixture()
def batch(db: Session) -> ReconciliationBatch:
    item = ReconciliationBatch(name="razorpay-live", status=BatchStatus.READY, currency="INR")
    db.add(item)
    db.commit()
    return item


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


class TestWebhookSignatureVerification:
    def test_valid_signature_is_accepted(self) -> None:
        body = b'{"event":"payment.captured"}'
        secret = "whsec_test"
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(secret=secret, body=body, signature=signature) is True

    def test_wrong_secret_is_rejected(self) -> None:
        body = b'{"event":"payment.captured"}'
        signature = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
        assert (
            verify_webhook_signature(secret="whsec_other", body=body, signature=signature) is False
        )

    def test_tampered_body_is_rejected(self) -> None:
        secret = "whsec_test"
        signature = hmac.new(
            secret.encode(), b'{"event":"payment.captured"}', hashlib.sha256
        ).hexdigest()
        tampered = b'{"event":"payment.captured","amount":999999999}'
        assert verify_webhook_signature(secret=secret, body=tampered, signature=signature) is False

    def test_no_secret_configured_fails_closed(self) -> None:
        body = b'{"event":"payment.captured"}'
        signature = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(secret=None, body=body, signature=signature) is False

    def test_no_signature_header_fails_closed(self) -> None:
        assert verify_webhook_signature(secret="whsec_test", body=b"{}", signature=None) is False


# ---------------------------------------------------------------------------
# JSON -> CSV translation, proven through the real ingestion path
# ---------------------------------------------------------------------------


class TestPaymentTranslation:
    def test_payment_amount_and_timestamp_round_trip_exactly(
        self, db: Session, batch: ReconciliationBatch
    ) -> None:
        # ₹1,234.56 in paise, plus a fee and tax Razorpay reports separately.
        payment = {
            "id": "pay_LiveTest001",
            "order_id": "order_LiveTest001",
            "amount": 123456,
            "currency": "INR",
            "fee": 2469,
            "tax": 377,
            "method": "upi",
            "created_at": 1_735_000_000,
            "status": "captured",
            "description": "Order payment",
        }
        result = ingest_file(
            db,
            batch=batch,
            source_kind=SourceKind.RAZORPAY_PAYMENTS,
            filename="sync.csv",
            payload=payments_to_csv([payment]),
        )
        assert result.accepted == 1
        record = db.execute(
            select(SourceRecord).where(SourceRecord.batch_id == batch.id)
        ).scalar_one()
        assert record.record_kind is RecordKind.PAYMENT
        assert record.payment_ref == "pay_LiveTest001"
        assert record.order_ref == "order_LiveTest001"
        assert record.amount_subunits == 123456
        assert record.fee_subunits == 2469
        assert record.tax_subunits == 377
        assert record.currency == "INR"
        assert record.occurred_at is not None
        assert int(record.occurred_at.timestamp()) == 1_735_000_000

    def test_multiple_payments_all_ingest(self, db: Session, batch: ReconciliationBatch) -> None:
        payments = [
            {
                "id": f"pay_{i}",
                "amount": 10000 * (i + 1),
                "currency": "INR",
                "created_at": 1_735_000_000 + i,
                "status": "captured",
            }
            for i in range(5)
        ]
        result = ingest_file(
            db,
            batch=batch,
            source_kind=SourceKind.RAZORPAY_PAYMENTS,
            filename="sync.csv",
            payload=payments_to_csv(payments),
        )
        assert result.accepted == 5


class TestRefundTranslation:
    def test_refund_round_trips_exactly(self, db: Session, batch: ReconciliationBatch) -> None:
        refund = {
            "id": "rfnd_LiveTest001",
            "payment_id": "pay_LiveTest001",
            "amount": 50000,
            "currency": "INR",
            "created_at": 1_735_000_500,
            "status": "processed",
            "notes": {"reason": "customer request"},
        }
        result = ingest_file(
            db,
            batch=batch,
            source_kind=SourceKind.RAZORPAY_REFUNDS,
            filename="sync.csv",
            payload=refunds_to_csv([refund]),
        )
        assert result.accepted == 1
        record = db.execute(
            select(SourceRecord).where(SourceRecord.batch_id == batch.id)
        ).scalar_one()
        assert record.record_kind is RecordKind.REFUND
        assert record.external_id == "rfnd_LiveTest001"
        assert record.payment_ref == "pay_LiveTest001"
        assert record.amount_subunits == 50000


class TestWebhookEventTranslation:
    def test_payment_captured_event_round_trips(
        self, db: Session, batch: ReconciliationBatch
    ) -> None:
        event = {
            "entity": "event",
            "event": "payment.captured",
            "created_at": 1_735_001_000,
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_WebhookTest001",
                        "amount": 75000,
                        "currency": "INR",
                    }
                }
            },
        }
        result = ingest_file(
            db,
            batch=batch,
            source_kind=SourceKind.WEBHOOK_EVENTS,
            filename="webhook.csv",
            payload=webhook_event_to_csv(event, event_id="evt_deterministic_001"),
        )
        assert result.accepted == 1
        record = db.execute(
            select(SourceRecord).where(SourceRecord.batch_id == batch.id)
        ).scalar_one()
        assert record.record_kind is RecordKind.EVENT
        assert record.external_id == "evt_deterministic_001"
        assert record.description == "payment.captured"
        assert record.payment_ref == "pay_WebhookTest001"
        assert record.amount_subunits == 75000


# ---------------------------------------------------------------------------
# POST /api/integrations/razorpay/sync
# ---------------------------------------------------------------------------


class TestSyncRoute:
    def test_sync_without_configured_keys_is_rejected(
        self, client: TestClient, batch: ReconciliationBatch
    ) -> None:
        response = client.post(
            "/api/integrations/razorpay/sync",
            json={
                "batch_id": batch.id,
                "from_time": "2026-01-01T00:00:00Z",
                "to_time": "2026-01-02T00:00:00Z",
            },
        )
        assert response.status_code == 503

    def test_sync_rejects_an_inverted_window(
        self,
        client: TestClient,
        batch: ReconciliationBatch,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_x")
        monkeypatch.setattr(settings, "razorpay_key_secret", "secret_x")
        response = client.post(
            "/api/integrations/razorpay/sync",
            json={
                "batch_id": batch.id,
                "from_time": "2026-01-02T00:00:00Z",
                "to_time": "2026-01-01T00:00:00Z",
            },
        )
        assert response.status_code == 400

    def test_sync_fetches_and_ingests_both_payments_and_refunds(
        self,
        client: TestClient,
        db: Session,
        batch: ReconciliationBatch,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_x")
        monkeypatch.setattr(settings, "razorpay_key_secret", "secret_x")
        monkeypatch.setattr(
            RazorpayClient,
            "fetch_payments",
            lambda self, *, from_ts, to_ts: [
                {"id": "pay_1", "amount": 100000, "currency": "INR", "created_at": from_ts + 10}
            ],
        )
        monkeypatch.setattr(
            RazorpayClient,
            "fetch_refunds",
            lambda self, *, from_ts, to_ts: [
                {
                    "id": "rfnd_1",
                    "payment_id": "pay_1",
                    "amount": 20000,
                    "currency": "INR",
                    "created_at": from_ts + 20,
                }
            ],
        )

        response = client.post(
            "/api/integrations/razorpay/sync",
            json={
                "batch_id": batch.id,
                "from_time": "2026-01-01T00:00:00Z",
                "to_time": "2026-01-02T00:00:00Z",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["payments"]["accepted_rows"] == 1
        assert body["refunds"]["accepted_rows"] == 1

        records = list(
            db.execute(select(SourceRecord).where(SourceRecord.batch_id == batch.id)).scalars()
        )
        assert {r.record_kind for r in records} == {RecordKind.PAYMENT, RecordKind.REFUND}

    def test_an_upstream_failure_leaves_nothing_partially_ingested(
        self,
        client: TestClient,
        db: Session,
        batch: ReconciliationBatch,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Payments fetch succeeds, refunds fetch fails: nothing is written.

        Both fetches happen before either is ingested, so an upstream failure
        on the second call cannot leave the first call's data sitting in the
        database with no matching refund pass.
        """
        monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_x")
        monkeypatch.setattr(settings, "razorpay_key_secret", "secret_x")
        monkeypatch.setattr(
            RazorpayClient,
            "fetch_payments",
            lambda self, *, from_ts, to_ts: [
                {"id": "pay_1", "amount": 100000, "currency": "INR", "created_at": from_ts + 10}
            ],
        )

        def _boom(self: RazorpayClient, *, from_ts: int, to_ts: int) -> list[dict]:
            raise RazorpayAPIError(500, "upstream error")

        monkeypatch.setattr(RazorpayClient, "fetch_refunds", _boom)

        response = client.post(
            "/api/integrations/razorpay/sync",
            json={
                "batch_id": batch.id,
                "from_time": "2026-01-01T00:00:00Z",
                "to_time": "2026-01-02T00:00:00Z",
            },
        )
        assert response.status_code == 502

        count = (
            db.execute(select(SourceRecord).where(SourceRecord.batch_id == batch.id))
            .scalars()
            .all()
        )
        assert count == []


# ---------------------------------------------------------------------------
# POST /api/webhooks/razorpay
# ---------------------------------------------------------------------------


def _signed_post(
    client: TestClient,
    *,
    batch_id: str,
    body: dict,
    secret: str,
    event_id: str | None = "evt_test_001",
) -> httpx.Response:
    raw = json.dumps(body).encode("utf-8")
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    headers = {"X-Razorpay-Signature": signature, "content-type": "application/json"}
    if event_id is not None:
        headers["X-Razorpay-Event-Id"] = event_id
    return client.post(f"/api/webhooks/razorpay?batch_id={batch_id}", content=raw, headers=headers)


class TestWebhookRoute:
    def test_unknown_batch_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/webhooks/razorpay?batch_id=does-not-exist",
            content=b"{}",
            headers={"X-Razorpay-Signature": "x"},
        )
        assert response.status_code == 404

    def test_no_webhook_secret_configured_fails_closed(
        self, client: TestClient, batch: ReconciliationBatch
    ) -> None:
        response = _signed_post(
            client, batch_id=batch.id, body={"event": "payment.captured"}, secret="whatever"
        )
        assert response.status_code == 401

    def test_wrong_signature_is_rejected(
        self,
        client: TestClient,
        batch: ReconciliationBatch,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "razorpay_webhook_secret", "correct_secret")
        response = _signed_post(
            client,
            batch_id=batch.id,
            body={"event": "payment.captured"},
            secret="wrong_secret",
        )
        assert response.status_code == 401

    def test_valid_signature_ingests_the_event(
        self,
        client: TestClient,
        db: Session,
        batch: ReconciliationBatch,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "razorpay_webhook_secret", "correct_secret")
        body = {
            "entity": "event",
            "event": "payment.captured",
            "created_at": 1_735_002_000,
            "payload": {
                "payment": {"entity": {"id": "pay_Webhook42", "amount": 42000, "currency": "INR"}}
            },
        }
        response = _signed_post(client, batch_id=batch.id, body=body, secret="correct_secret")
        assert response.status_code == 201
        payload = response.json()
        assert payload["accepted"] is True
        assert payload["duplicate"] is False

        record = db.execute(
            select(SourceRecord).where(SourceRecord.batch_id == batch.id)
        ).scalar_one()
        assert record.external_id == "evt_test_001"
        assert record.amount_subunits == 42000

    def test_a_replayed_delivery_is_idempotent(
        self,
        client: TestClient,
        db: Session,
        batch: ReconciliationBatch,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "razorpay_webhook_secret", "correct_secret")
        body = {
            "entity": "event",
            "event": "payment.captured",
            "created_at": 1_735_002_100,
            "payload": {
                "payment": {"entity": {"id": "pay_Replay1", "amount": 15000, "currency": "INR"}}
            },
        }
        first = _signed_post(
            client, batch_id=batch.id, body=body, secret="correct_secret", event_id="evt_replay_1"
        )
        second = _signed_post(
            client, batch_id=batch.id, body=body, secret="correct_secret", event_id="evt_replay_1"
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["accepted"] is True
        assert second.json()["duplicate"] is True

        records = (
            db.execute(select(SourceRecord).where(SourceRecord.batch_id == batch.id))
            .scalars()
            .all()
        )
        assert len(records) == 1

    def test_missing_event_id_header_still_ingests_via_derived_id(
        self,
        client: TestClient,
        db: Session,
        batch: ReconciliationBatch,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "razorpay_webhook_secret", "correct_secret")
        body = {
            "entity": "event",
            "event": "refund.processed",
            "created_at": 1_735_002_200,
            "payload": {
                "refund": {"entity": {"id": "rfnd_NoHeader1", "amount": 5000, "currency": "INR"}}
            },
        }
        response = _signed_post(
            client, batch_id=batch.id, body=body, secret="correct_secret", event_id=None
        )
        assert response.status_code == 201
        assert response.json()["accepted"] is True
