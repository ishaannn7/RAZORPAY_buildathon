"""File ingestion: parse, validate, normalize, persist.

Failure policy has two tiers, because "fail closed" and "reject every row over
one bad cell" are different things:

* A *structural* fault — unreadable header, a missing required column, or a
  row-level error rate above :data:`MAX_ROW_ERROR_RATE` — rejects the entire
  file. Nothing is written. A changed export format is a structural fault, and
  loading half of it would silently understate the ledger.
* An *isolated* row fault rejects that row, records why, and continues. The row
  appears in the file's ``validation_errors`` so the unexplained amount stays
  attributable rather than vanishing.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from reconproof.audit.log import record as audit_record
from reconproof.config import get_settings
from reconproof.db.models import ReconciliationBatch, SourceFile, SourceRecord
from reconproof.domain.entities import (
    Actor,
    AuditAction,
    PaymentMethod,
    RecordStatus,
    SourceKind,
)
from reconproof.domain.money import Money, looks_like_unit_confusion
from reconproof.ingest import parsers
from reconproof.ingest.keys import dedupe_key, normalize_description, normalize_reference
from reconproof.ingest.schemas import SourceSchema, schema_for

#: Above this fraction of unparseable rows the file is treated as structurally
#: wrong rather than merely dirty.
MAX_ROW_ERROR_RATE = 0.05

#: Rupee-scale sanity bound. A single INR row above ~₹100 crore in this dataset
#: is far more likely a unit error than a real transaction, so it is flagged for
#: review rather than accepted.
IMPLAUSIBLE_AMOUNT_SUBUNITS = 100_00_00_000 * 100


@dataclass(slots=True)
class RowError:
    row_number: int
    column: str | None
    message: str
    raw_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row_number,
            "column": self.column,
            "message": self.message,
            "value": self.raw_value,
        }


@dataclass(slots=True)
class IngestResult:
    source_file: SourceFile | None
    accepted: int = 0
    rejected: int = 0
    duplicates_collapsed: int = 0
    errors: list[RowError] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    detected_columns: dict[str, str] = field(default_factory=dict)
    structural_failure: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.structural_failure is None


class StructuralIngestError(Exception):
    """Raised when a file cannot be ingested at all. Aborts the transaction."""

    def __init__(self, message: str, errors: list[RowError] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or []


def compute_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_rows(payload: bytes) -> tuple[list[str], list[list[str]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = payload.decode("latin-1")
        except UnicodeDecodeError as exc:  # pragma: no cover - defensive
            raise StructuralIngestError(f"file is not decodable text: {exc}") from exc

    stripped = text.lstrip()
    if not stripped:
        raise StructuralIngestError("file is empty")

    first_line = stripped.splitlines()[0]
    delimiter = parsers.sniff_delimiter(first_line)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise StructuralIngestError(f"malformed CSV: {exc}") from exc

    if not rows:
        raise StructuralIngestError("file contains no rows")
    headers = [cell.strip() for cell in rows[0]]
    if not any(headers):
        raise StructuralIngestError("header row is blank")
    return headers, rows[1:]


def _normalize_row(
    schema: SourceSchema,
    mapping: dict[str, int],
    cells: list[str],
    row_number: int,
    batch_currency: str,
) -> tuple[dict[str, Any], list[str]]:
    """Turn one raw row into canonical field values.

    Returns the canonical payload and a list of warning tags describing what had
    to be assumed or looked suspicious.
    """

    def cell(name: str) -> str | None:
        index = mapping.get(name)
        if index is None or index >= len(cells):
            return None
        return cells[index]

    warnings: list[str] = []
    raw: dict[str, str] = {}
    for spec in schema.fields:
        value = cell(spec.name)
        if value is not None and value.strip():
            raw[spec.name] = value.strip()

    currency = parsers.parse_currency(cell("currency"), batch_currency)

    amount_cell = cell(schema.amount_field)
    amount = parsers.parse_money(amount_cell, currency)
    assert amount is not None  # parse_money raises rather than returning None here

    if abs(amount.subunits) > IMPLAUSIBLE_AMOUNT_SUBUNITS:
        warnings.append("implausible_amount")

    timestamp = parsers.parse_timestamp(cell(schema.timestamp_field), allow_blank=True)
    if timestamp is None:
        warnings.append("missing_timestamp")
    else:
        if timestamp.assumed_utc:
            warnings.append("assumed_utc")
        if timestamp.assumed_day_first:
            warnings.append("assumed_day_first")

    fee = parsers.parse_money(cell("fee"), currency, allow_blank=True)
    tax = parsers.parse_money(cell("tax"), currency, allow_blank=True)
    gross = parsers.parse_money(cell("gross_amount"), currency, allow_blank=True)
    refund_total = parsers.parse_money(cell("refund_total"), currency, allow_blank=True)
    net_from_file = parsers.parse_money(cell("net_amount"), currency, allow_blank=True)

    description_parts = [raw[name] for name in schema.description_fields if name in raw] or (
        [raw["description"]] if "description" in raw else []
    )
    description = " | ".join(description_parts) or None

    bank_ref = parsers.parse_text(cell("bank_ref"))
    order_ref = parsers.parse_text(cell("order_ref"))
    payment_ref = parsers.parse_text(cell("payment_ref"))
    settlement_ref = parsers.parse_text(cell("settlement_ref"))
    explicit_id = parsers.parse_text(cell("external_id"))

    identity_field = schema.identity_field
    external_id = None
    if identity_field:
        external_id = {
            "external_id": explicit_id,
            "order_ref": order_ref,
            "payment_ref": payment_ref,
            "settlement_ref": settlement_ref,
            "bank_ref": bank_ref,
        }.get(identity_field)

    # A settlement row states its own net; when the arithmetic disagrees with
    # the components the row is kept as-is and flagged. Silently recomputing
    # would erase the discrepancy that reconciliation exists to surface.
    if gross is not None and net_from_file is not None:
        components = gross
        for deduction in (fee, tax, refund_total):
            if deduction is not None:
                components = components - deduction
        if components != net_from_file:
            warnings.append("net_amount_recompute_mismatch")

    if fee is not None and looks_like_unit_confusion(amount, fee):
        warnings.append("possible_unit_confusion")

    payload: dict[str, Any] = {
        "external_id": external_id,
        "order_ref": order_ref,
        "payment_ref": payment_ref,
        "settlement_ref": settlement_ref,
        "bank_ref": bank_ref,
        "bank_ref_normalized": normalize_reference(bank_ref),
        "amount_subunits": amount.subunits,
        "currency": amount.currency,
        "fee_subunits": fee.subunits if fee else None,
        "tax_subunits": tax.subunits if tax else None,
        "net_subunits": net_from_file.subunits if net_from_file else None,
        "gross_subunits": gross.subunits if gross else None,
        "refund_total_subunits": refund_total.subunits if refund_total else None,
        "occurred_at": timestamp.value if timestamp else None,
        "timestamp_is_date_only": bool(timestamp and timestamp.date_only),
        "description": description,
        "description_normalized": normalize_description(description),
        "counterparty": parsers.parse_text(cell("counterparty")),
        "payment_method": parsers.parse_payment_method(cell("payment_method")),
        "status": parsers.parse_status(cell("status")),
        "source_row_number": row_number,
        "raw": raw,
    }
    if currency != amount.currency:  # pragma: no cover - defensive
        warnings.append("currency_normalized")
    return payload, warnings


def ingest_file(
    session: Session,
    *,
    batch: ReconciliationBatch,
    source_kind: SourceKind,
    filename: str,
    payload: bytes,
    truth_lookup: dict[str, Any] | None = None,
) -> IngestResult:
    """Ingest one source file into *batch*.

    Raises :class:`StructuralIngestError` on a structural fault so the caller's
    transaction rolls back and the batch is left exactly as it was.
    """
    settings = get_settings()
    if len(payload) > settings.max_upload_bytes:
        raise StructuralIngestError(
            f"file exceeds maximum size of {settings.max_upload_bytes} bytes"
        )

    digest = compute_sha256(payload)
    existing = session.execute(
        select(SourceFile).where(
            SourceFile.batch_id == batch.id, SourceFile.content_sha256 == digest
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Byte-identical re-upload. Recorded and ignored rather than merged, so
        # a double-click cannot inflate the ledger.
        audit_record(
            session,
            action=AuditAction.DUPLICATE_UPLOAD_IGNORED,
            actor=Actor.SYSTEM,
            batch_id=batch.id,
            subject_type="source_file",
            subject_id=existing.id,
            input_sha256=digest,
            message=f"{filename} is byte-identical to an already ingested file; ignored",
        )
        return IngestResult(
            source_file=existing,
            duplicates_collapsed=existing.row_count,
            warnings=["duplicate_file_ignored"],
        )

    headers, data_rows = _read_rows(payload)
    schema = schema_for(source_kind)
    mapping, unmapped = schema.resolve_columns(headers)
    missing = schema.missing_required(mapping)
    if missing:
        raise StructuralIngestError(
            f"{filename}: missing required column(s) for {source_kind.value}: "
            f"{', '.join(missing)}. Found headers: {', '.join(headers)}"
        )

    stored_path = settings.upload_dir / f"{digest}{_suffix(filename)}"
    if not stored_path.exists():
        stored_path.write_bytes(payload)

    source_file = SourceFile(
        batch_id=batch.id,
        source_kind=source_kind,
        filename=filename,
        content_sha256=digest,
        byte_size=len(payload),
        stored_path=str(stored_path),
        row_count=len(data_rows),
        detected_columns={
            headers[index]: name for name, index in mapping.items() if index < len(headers)
        },
    )
    session.add(source_file)
    session.flush()

    errors: list[RowError] = []
    warnings: set[str] = set()
    seen_keys: set[str] = set()
    accepted = 0
    collapsed = 0
    pending: list[SourceRecord] = []

    existing_keys = set(
        session.execute(select(SourceRecord.dedupe_key).where(SourceRecord.batch_id == batch.id))
        .scalars()
        .all()
    )

    for offset, cells in enumerate(data_rows):
        row_number = offset + 2  # 1-indexed, plus the header row
        if not any(cell.strip() for cell in cells):
            continue
        try:
            canonical, row_warnings = _normalize_row(
                schema, mapping, cells, row_number, batch.currency
            )
        except parsers.ParseError as exc:
            errors.append(RowError(row_number=row_number, column=None, message=str(exc)))
            continue

        warnings.update(row_warnings)
        key = dedupe_key(source_kind, canonical["external_id"], fallback=cells)
        if key in seen_keys or key in existing_keys:
            # Same natural key: a replayed delivery. Collapsed, and the fact is
            # recorded so the count can be shown in the UI.
            collapsed += 1
            continue
        seen_keys.add(key)

        record = SourceRecord(
            batch_id=batch.id,
            source_file_id=source_file.id,
            source_kind=source_kind,
            record_kind=schema.record_kind,
            dedupe_key=key,
            **canonical,
        )
        if truth_lookup:
            record.truth_group = truth_lookup.get("groups", {}).get(key)
            record.truth_corruptions = truth_lookup.get("corruptions", {}).get(key, [])
        pending.append(record)
        accepted += 1

    considered = accepted + collapsed + len(errors)
    if considered and len(errors) / considered > MAX_ROW_ERROR_RATE:
        raise StructuralIngestError(
            f"{filename}: {len(errors)} of {considered} rows failed to parse "
            f"({len(errors) / considered:.1%}), above the {MAX_ROW_ERROR_RATE:.0%} "
            "threshold. The file is treated as structurally invalid and nothing "
            "was imported.",
            errors=errors,
        )

    session.add_all(pending)
    source_file.accepted_rows = accepted
    source_file.rejected_rows = len(errors)
    source_file.validation_errors = [error.to_dict() for error in errors]
    session.flush()

    audit_record(
        session,
        action=AuditAction.SOURCE_UPLOADED,
        actor=Actor.SYSTEM,
        batch_id=batch.id,
        subject_type="source_file",
        subject_id=source_file.id,
        input_sha256=digest,
        detail={
            "source_kind": source_kind.value,
            "accepted": accepted,
            "rejected": len(errors),
            "duplicates_collapsed": collapsed,
            "unmapped_columns": unmapped,
            "warnings": sorted(warnings),
        },
        message=f"Ingested {accepted} records from {filename}",
    )

    return IngestResult(
        source_file=source_file,
        accepted=accepted,
        rejected=len(errors),
        duplicates_collapsed=collapsed,
        errors=errors,
        unmapped_columns=unmapped,
        detected_columns=source_file.detected_columns,
        warnings=sorted(warnings),
    )


def _suffix(filename: str) -> str:
    lowered = filename.lower()
    for suffix in (".csv", ".json", ".jsonl", ".txt"):
        if lowered.endswith(suffix):
            return suffix
    return ".dat"


def to_money(record: SourceRecord) -> Money:
    return Money(record.amount_subunits, record.currency)


def record_fee(record: SourceRecord) -> Money:
    return Money(record.fee_subunits or 0, record.currency)


def record_tax(record: SourceRecord) -> Money:
    return Money(record.tax_subunits or 0, record.currency)


def record_timestamp(record: SourceRecord) -> datetime | None:
    return record.occurred_at


__all__ = [
    "IngestResult",
    "PaymentMethod",
    "RecordStatus",
    "RowError",
    "StructuralIngestError",
    "compute_sha256",
    "ingest_file",
    "record_fee",
    "record_tax",
    "record_timestamp",
    "to_money",
]
