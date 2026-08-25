"""Razorpay adapter: pull sync and push webhook, both feeding the same
CSV-shaped ingestion path everything else in this codebase already trusts.

Rather than teaching the pipeline a second, JSON-native ingestion route, both
entry points translate Razorpay's API/webhook JSON into the exact CSV column
names :mod:`reconproof.ingest.schemas` already expects for
``razorpay_payments``, ``razorpay_refunds`` and ``webhook_events``, then hand
the bytes to :func:`reconproof.ingest.loader.ingest_file`. That gets dedup,
row-level validation, structural-failure handling and the audit trail for
free, and it means a live sync and a bulk CSV upload of the same data produce
identical `SourceRecord` rows.

Amounts arrive from Razorpay as integer paise; the ingestion CSV format
expects a major-unit decimal cell (``"500.00"``), so every amount is
round-tripped through :class:`~reconproof.domain.money.Money` rather than
divided by 100 as a float, which would reintroduce the rounding error the
domain layer exists to avoid.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
from datetime import UTC, datetime
from typing import Any

import httpx

from reconproof.domain.money import Money

#: Hard stop on pagination so a misbehaving API (or an unbounded date range)
#: cannot spin this into an unattended, unlimited fetch. 50 pages of 100 is
#: 5,000 records per call, generous for a demo sync.
MAX_PAGES = 50
PAGE_SIZE = 100


class RazorpayConfigError(RuntimeError):
    """Raised when a sync or webhook call is attempted without credentials."""


class RazorpayAPIError(RuntimeError):
    """Raised when Razorpay's API responds with a non-2xx status."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Razorpay API returned {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


def verify_webhook_signature(*, secret: str | None, body: bytes, signature: str | None) -> bool:
    """Verify ``X-Razorpay-Signature`` against the raw request body.

    Per Razorpay's documented scheme: HMAC-SHA256 of the raw (unparsed) body,
    keyed by the webhook secret, hex-encoded, compared to the header value.
    Fails closed: no secret configured or no signature header means "not
    verified", not "trust it anyway" — the same posture the policy engine
    takes when its document is unavailable.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _money_cell(subunits: int | None, currency: str) -> str:
    if subunits is None:
        return ""
    return Money(subunits, currency).format()


def _epoch_cell(epoch_seconds: int | None) -> str:
    if epoch_seconds is None:
        return ""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


def _write_csv(rows: list[dict[str, str]], headers: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


class RazorpayClient:
    """Thin, synchronous wrapper over the Razorpay Payments REST API.

    Synchronous because every other route handler that isn't doing file I/O in
    this codebase is a plain ``def``, not ``async def`` — matching that rather
    than introducing the only async HTTP call in the API surface.
    """

    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        base_url: str,
        timeout: float,
    ) -> None:
        self._key_id = key_id
        self._key_secret = key_secret
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _get_all(self, path: str, *, from_ts: int, to_ts: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        skip = 0
        with httpx.Client(
            auth=httpx.BasicAuth(self._key_id, self._key_secret), timeout=self._timeout
        ) as client:
            for _page in range(MAX_PAGES):
                response = client.get(
                    f"{self._base_url}{path}",
                    params={"from": from_ts, "to": to_ts, "count": PAGE_SIZE, "skip": skip},
                )
                if response.status_code >= 400:
                    raise RazorpayAPIError(response.status_code, response.text)
                payload = response.json()
                page_items = payload.get("items", [])
                items.extend(page_items)
                if len(page_items) < PAGE_SIZE:
                    break
                skip += PAGE_SIZE
        return items

    def fetch_payments(self, *, from_ts: int, to_ts: int) -> list[dict[str, Any]]:
        return self._get_all("/payments", from_ts=from_ts, to_ts=to_ts)

    def fetch_refunds(self, *, from_ts: int, to_ts: int) -> list[dict[str, Any]]:
        return self._get_all("/refunds", from_ts=from_ts, to_ts=to_ts)


def payments_to_csv(payments: list[dict[str, Any]]) -> bytes:
    headers = [
        "payment_ref",
        "order_ref",
        "amount",
        "currency",
        "fee",
        "tax",
        "payment_method",
        "occurred_at",
        "status",
        "description",
    ]
    rows = []
    for payment in payments:
        currency = payment.get("currency") or "INR"
        rows.append(
            {
                "payment_ref": payment.get("id", ""),
                "order_ref": payment.get("order_id") or "",
                "amount": _money_cell(payment.get("amount"), currency),
                "currency": currency,
                "fee": _money_cell(payment.get("fee"), currency),
                "tax": _money_cell(payment.get("tax"), currency),
                "payment_method": payment.get("method") or "",
                "occurred_at": _epoch_cell(payment.get("created_at")),
                "status": payment.get("status") or "",
                "description": payment.get("description") or "",
            }
        )
    return _write_csv(rows, headers)


def refunds_to_csv(refunds: list[dict[str, Any]]) -> bytes:
    headers = [
        "external_id",
        "payment_ref",
        "amount",
        "currency",
        "occurred_at",
        "settlement_ref",
        "status",
        "description",
    ]
    rows = []
    for refund in refunds:
        currency = refund.get("currency") or "INR"
        notes = refund.get("notes")
        description = (
            ", ".join(f"{k}: {v}" for k, v in notes.items()) if isinstance(notes, dict) else ""
        )
        rows.append(
            {
                "external_id": refund.get("id", ""),
                "payment_ref": refund.get("payment_id") or "",
                "amount": _money_cell(refund.get("amount"), currency),
                "currency": currency,
                "occurred_at": _epoch_cell(refund.get("created_at")),
                "settlement_ref": "",
                "status": refund.get("status") or "",
                "description": description,
            }
        )
    return _write_csv(rows, headers)


def _webhook_entity(payload: dict[str, Any], *kinds: str) -> dict[str, Any] | None:
    """The first present ``payload.<kind>.entity`` object, e.g. ``payment``."""
    for kind in kinds:
        container = payload.get(kind)
        if isinstance(container, dict):
            entity = container.get("entity")
            if isinstance(entity, dict):
                return entity
    return None


def webhook_event_to_csv(event: dict[str, Any], *, event_id: str) -> bytes:
    """One-row CSV for a single webhook delivery, shaped as ``webhook_events``.

    ``event_id`` should be the ``x-razorpay-event-id`` header when Razorpay
    sends one (it is documented as unique per event, which is exactly the
    natural key ``ingest_file``'s dedup needs to collapse a retried
    delivery); the caller falls back to a derived id when that header is
    absent, which is a weaker guarantee and is flagged at the call site.
    """
    payload = event.get("payload") or {}
    entity = _webhook_entity(payload, "payment", "refund", "settlement")
    currency = (entity.get("currency") if entity else None) or "INR"
    amount = entity.get("amount") if entity else None
    payment_ref = ""
    if entity is not None:
        payment_ref = entity.get("id") if "payment_id" not in entity else entity.get("payment_id")
        payment_ref = payment_ref or ""

    row = {
        "external_id": event_id,
        "description": event.get("event") or "",
        "payment_ref": payment_ref,
        "amount": _money_cell(amount, currency),
        "currency": currency,
        "occurred_at": _epoch_cell(event.get("created_at")),
        "status": "",
    }
    return _write_csv(
        [row],
        [
            "external_id",
            "description",
            "payment_ref",
            "amount",
            "currency",
            "occurred_at",
            "status",
        ],
    )
