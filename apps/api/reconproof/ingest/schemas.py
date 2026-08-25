"""Source schemas and column mapping.

Each source names the same concept differently: a payment reference is
``payment_id`` in a provider export, ``Chq/Ref No`` in a bank statement and
``txn ref`` in a hand-maintained ledger. Rather than requiring the user to
rename columns before upload, each schema declares the aliases it accepts and
mapping is resolved once per file.
"""

from __future__ import annotations

from dataclasses import dataclass

from reconproof.domain.entities import RecordKind, SourceKind
from reconproof.ingest.keys import normalize_reference


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    aliases: tuple[str, ...] = ()
    required: bool = False
    #: Semantic role, used by normalization to decide how to parse the cell.
    role: str = "text"

    def matches(self, header: str) -> bool:
        target = normalize_reference(header)
        if target is None:
            return False
        candidates = {normalize_reference(self.name)}
        candidates.update(normalize_reference(alias) for alias in self.aliases)
        return target in candidates


@dataclass(frozen=True, slots=True)
class SourceSchema:
    source_kind: SourceKind
    record_kind: RecordKind
    fields: tuple[FieldSpec, ...]
    #: Field whose value becomes the record's stable external identifier.
    identity_field: str | None = None
    #: Field carrying the record's primary monetary value.
    amount_field: str = "amount"
    #: Field carrying the record's effective timestamp.
    timestamp_field: str = "occurred_at"
    description_fields: tuple[str, ...] = ()

    def resolve_columns(self, headers: list[str]) -> tuple[dict[str, int], list[str]]:
        """Map canonical field names onto column indices.

        Returns the mapping and the list of headers that matched nothing. An
        unmapped column is reported rather than dropped silently, because an
        unrecognized column is often the first symptom of a changed export
        format.
        """
        mapping: dict[str, int] = {}
        claimed: set[int] = set()
        for spec in self.fields:
            for index, header in enumerate(headers):
                if index in claimed:
                    continue
                if spec.matches(header):
                    mapping[spec.name] = index
                    claimed.add(index)
                    break
        unmapped = [
            header
            for index, header in enumerate(headers)
            if index not in claimed and header.strip()
        ]
        return mapping, unmapped

    def missing_required(self, mapping: dict[str, int]) -> list[str]:
        return [spec.name for spec in self.fields if spec.required and spec.name not in mapping]


_AMOUNT = "amount"
_TIME = "timestamp"
_REF = "reference"
_MONEY = "money"

SCHEMAS: dict[SourceKind, SourceSchema] = {
    SourceKind.ORDER_LEDGER: SourceSchema(
        source_kind=SourceKind.ORDER_LEDGER,
        record_kind=RecordKind.ORDER,
        identity_field="order_ref",
        amount_field="amount",
        timestamp_field="occurred_at",
        description_fields=("description", "counterparty"),
        fields=(
            FieldSpec("order_ref", ("order id", "order_id", "order no", "orderref"), True, _REF),
            FieldSpec("amount", ("order amount", "order_amount", "total", "value"), True, _AMOUNT),
            FieldSpec("currency", ("ccy", "curr"), False, "currency"),
            FieldSpec(
                "occurred_at", ("created at", "created_at", "order date", "date"), True, _TIME
            ),
            FieldSpec("counterparty", ("customer", "customer name", "buyer"), False, "text"),
            FieldSpec("status", ("state", "order status"), False, "status"),
            FieldSpec("description", ("notes", "remarks", "memo"), False, "text"),
        ),
    ),
    SourceKind.RAZORPAY_PAYMENTS: SourceSchema(
        source_kind=SourceKind.RAZORPAY_PAYMENTS,
        record_kind=RecordKind.PAYMENT,
        identity_field="payment_ref",
        fields=(
            FieldSpec("payment_ref", ("payment id", "payment_id", "txn id"), True, _REF),
            FieldSpec("order_ref", ("order id", "order_id"), False, _REF),
            FieldSpec("amount", ("amount", "captured amount", "gross"), True, _AMOUNT),
            FieldSpec("currency", ("ccy", "curr"), False, "currency"),
            FieldSpec("fee", ("fee", "commission", "mdr"), False, _MONEY),
            FieldSpec("tax", ("tax", "gst", "service tax"), False, _MONEY),
            FieldSpec("payment_method", ("method", "mode", "instrument"), False, "method"),
            FieldSpec(
                "occurred_at", ("captured at", "captured_at", "created at", "date"), True, _TIME
            ),
            FieldSpec("status", ("status", "state"), False, "status"),
            FieldSpec("description", ("description", "notes", "narration"), False, "text"),
        ),
    ),
    SourceKind.RAZORPAY_REFUNDS: SourceSchema(
        source_kind=SourceKind.RAZORPAY_REFUNDS,
        record_kind=RecordKind.REFUND,
        identity_field="external_id",
        fields=(
            FieldSpec("external_id", ("refund id", "refund_id"), True, _REF),
            FieldSpec("payment_ref", ("payment id", "payment_id"), False, _REF),
            FieldSpec("amount", ("amount", "refund amount"), True, _AMOUNT),
            FieldSpec("currency", ("ccy", "curr"), False, "currency"),
            FieldSpec(
                "occurred_at", ("created at", "created_at", "refunded at", "date"), True, _TIME
            ),
            FieldSpec("settlement_ref", ("settlement id", "settlement_id"), False, _REF),
            FieldSpec(
                "status", ("status", "state", "speed processed", "speed_processed"), False, "status"
            ),
            FieldSpec("description", ("notes", "remarks"), False, "text"),
        ),
    ),
    SourceKind.SETTLEMENT_REPORT: SourceSchema(
        source_kind=SourceKind.SETTLEMENT_REPORT,
        record_kind=RecordKind.SETTLEMENT,
        identity_field="settlement_ref",
        # The settlement's own amount is its *net* credited value: that is the
        # figure the bank actually pays and therefore the figure that must
        # reconcile against the statement.
        amount_field="net_amount",
        fields=(
            FieldSpec("settlement_ref", ("settlement id", "settlement_id"), True, _REF),
            FieldSpec("gross_amount", ("gross amount", "gross_amount", "gross"), False, _MONEY),
            FieldSpec("fee", ("fees", "fee", "commission"), False, _MONEY),
            FieldSpec("tax", ("tax", "gst"), False, _MONEY),
            FieldSpec("refund_total", ("refunds", "refund total", "refund_total"), False, _MONEY),
            FieldSpec(
                "net_amount", ("net settled", "net_settled", "net amount", "net"), True, _AMOUNT
            ),
            FieldSpec("currency", ("ccy", "curr"), False, "currency"),
            FieldSpec(
                "occurred_at", ("settled on", "settled_on", "settled at", "date"), True, _TIME
            ),
            FieldSpec("bank_ref", ("utr", "rrn", "bank reference"), False, _REF),
            FieldSpec("description", ("txn count", "txn_count", "notes"), False, "text"),
        ),
    ),
    SourceKind.BANK_STATEMENT: SourceSchema(
        source_kind=SourceKind.BANK_STATEMENT,
        record_kind=RecordKind.BANK_CREDIT,
        identity_field="external_id",
        amount_field="amount",
        description_fields=("description",),
        fields=(
            FieldSpec("external_id", ("txn id", "transaction id", "sl no"), False, _REF),
            FieldSpec("occurred_at", ("value date", "value_date", "txn date", "date"), True, _TIME),
            FieldSpec("description", ("narration", "particulars", "description"), False, "text"),
            FieldSpec("bank_ref", ("chq/ref no", "ref no", "reference", "utr"), False, _REF),
            FieldSpec("amount", ("credit", "credit amount", "deposit", "amount"), True, _AMOUNT),
            FieldSpec("currency", ("ccy", "curr"), False, "currency"),
            FieldSpec("net_amount", ("closing balance", "balance"), False, _MONEY),
        ),
    ),
    SourceKind.FEE_REPORT: SourceSchema(
        source_kind=SourceKind.FEE_REPORT,
        record_kind=RecordKind.FEE,
        identity_field="external_id",
        amount_field="fee",
        fields=(
            FieldSpec("external_id", ("fee id", "fee_id"), True, _REF),
            FieldSpec("payment_ref", ("payment id", "payment_id"), False, _REF),
            FieldSpec("settlement_ref", ("settlement id", "settlement_id"), False, _REF),
            FieldSpec("fee", ("commission", "fee", "mdr"), True, _AMOUNT),
            FieldSpec("tax", ("gst", "tax"), False, _MONEY),
            FieldSpec("currency", ("ccy", "curr"), False, "currency"),
            FieldSpec("occurred_at", ("charged on", "charged_on", "date"), True, _TIME),
        ),
    ),
    SourceKind.WEBHOOK_EVENTS: SourceSchema(
        source_kind=SourceKind.WEBHOOK_EVENTS,
        record_kind=RecordKind.EVENT,
        identity_field="external_id",
        fields=(
            FieldSpec("external_id", ("event id", "event_id"), True, _REF),
            FieldSpec("description", ("event", "event type", "type"), False, "text"),
            FieldSpec("payment_ref", ("payment id", "payment_id"), False, _REF),
            FieldSpec("amount", ("amount",), True, _AMOUNT),
            FieldSpec("currency", ("ccy", "curr"), False, "currency"),
            FieldSpec("occurred_at", ("created at", "created_at", "date"), True, _TIME),
            FieldSpec("status", ("delivery attempt", "delivery_attempt"), False, "text"),
        ),
    ),
}


def schema_for(source_kind: SourceKind) -> SourceSchema:
    return SCHEMAS[source_kind]


def detect_source_kind(headers: list[str]) -> tuple[SourceKind | None, float]:
    """Guess which source a file came from by scoring header overlap.

    Used to pre-fill the source selector on upload. The user's explicit choice
    always wins: a wrong guess must not silently reinterpret a file.
    """
    best: SourceKind | None = None
    best_score = 0.0
    for source_kind, schema in SCHEMAS.items():
        mapping, _ = schema.resolve_columns(headers)
        required = [spec.name for spec in schema.fields if spec.required]
        if any(name not in mapping for name in required):
            continue
        score = len(mapping) / max(len(schema.fields), 1)
        # Reward distinctive matches: a bank statement's "narration" is far more
        # diagnostic than a shared "currency" column.
        if score > best_score:
            best, best_score = source_kind, score
    return best, round(best_score, 3)
