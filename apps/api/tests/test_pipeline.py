"""End-to-end pipeline behaviour on generated data.

These are the guarantees the product claims. They are asserted against a real
ingested batch rather than mocks, because the claims are about the system as a
whole: that no unexplained rupee escapes the queue, that a replay is
deterministic, and that a duplicate upload cannot move a balance.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reconproof.config import Settings
from reconproof.db.models import (
    AccountingCheck,
    AuditEvent,
    EvidenceItem,
    MatchCandidate,
    ReconciliationBatch,
    ReconciliationException,
    ReconciliationMatch,
    SourceRecord,
)
from reconproof.domain.entities import (
    AuditAction,
    BatchStatus,
    MatchDecision,
    RecordKind,
    SourceKind,
)
from reconproof.ingest.loader import StructuralIngestError, ingest_file
from reconproof.learning.training import materialize_dataset
from reconproof.pipeline import ReconciliationMetrics, ReconciliationPipeline
from reconproof.synthetic.generator import DatasetSpec, write_dataset


@pytest.fixture()
def reconciled(
    db: Session, settings: Settings, tmp_path: Path
) -> tuple[ReconciliationBatch, object]:
    """A modest batch, reconciled without a trained model.

    Running with no scorer is deliberate: it proves the deterministic core works
    on its own, which is the fallback path when no model or LLM is available.
    """
    batch, _truth = materialize_dataset(
        db,
        DatasetSpec(name="e2e", seed=4242, n_orders=300),
        tmp_path / "data",
    )
    result = ReconciliationPipeline(db, settings=settings).run(batch)
    db.commit()
    return batch, result


class TestUnresolvedValueAccounting:
    def test_every_unexplained_rupee_is_in_the_queue(
        self, reconciled: tuple[ReconciliationBatch, object]
    ) -> None:
        """The queue's total must equal the unexplained amount exactly.

        If a break existed but no exception carried its value, the queue would
        understate what is at stake, which is the one failure a finance team
        cannot detect on its own.
        """
        _batch, result = reconciled
        metrics = result.metrics  # type: ignore[attr-defined]
        assert metrics.exception_subunits == metrics.unexplained_subunits
        assert metrics.unresolved_value_fully_represented

    def test_exception_amounts_sum_to_reported_total(
        self, db: Session, reconciled: tuple[ReconciliationBatch, object]
    ) -> None:
        batch, result = reconciled
        stored = db.execute(
            select(func.coalesce(func.sum(ReconciliationException.amount_subunits), 0)).where(
                ReconciliationException.batch_id == batch.id
            )
        ).scalar_one()
        assert stored == result.metrics.exception_subunits  # type: ignore[attr-defined]

    def test_every_exception_has_a_subject_and_summary(
        self, db: Session, reconciled: tuple[ReconciliationBatch, object]
    ) -> None:
        batch, _ = reconciled
        exceptions = list(
            db.execute(
                select(ReconciliationException).where(ReconciliationException.batch_id == batch.id)
            ).scalars()
        )
        assert exceptions, "a realistic batch should surface at least one exception"
        for item in exceptions:
            assert item.subject_record_id
            assert item.summary.strip()
            assert item.amount_subunits >= 0


class TestAutomaticDecisions:
    def test_no_accepted_match_violates_an_invariant(
        self, db: Session, reconciled: tuple[ReconciliationBatch, object]
    ) -> None:
        """The core safety claim: automation never posts an impossible link."""
        batch, _ = reconciled
        accepted = list(
            db.execute(
                select(ReconciliationMatch).where(
                    ReconciliationMatch.batch_id == batch.id,
                    ReconciliationMatch.decision == MatchDecision.AUTO_ACCEPTED,
                )
            ).scalars()
        )
        assert accepted, "exact matching should accept something on clean data"
        for match in accepted:
            assert match.invariants_passed
            assert not [name for name in match.invariants_failed if name], (
                f"{match.relation} was accepted with failing checks: {match.invariants_failed}"
            )

    def test_accepted_matches_never_cross_currencies(
        self, db: Session, reconciled: tuple[ReconciliationBatch, object]
    ) -> None:
        batch, _ = reconciled
        rows = db.execute(
            select(ReconciliationMatch, SourceRecord)
            .join(SourceRecord, SourceRecord.id == ReconciliationMatch.left_record_id)
            .where(
                ReconciliationMatch.batch_id == batch.id,
                ReconciliationMatch.decision == MatchDecision.AUTO_ACCEPTED,
            )
        ).all()
        for match, record in rows:
            assert match.currency == record.currency

    def test_run_completes_and_is_audited(
        self, db: Session, reconciled: tuple[ReconciliationBatch, object]
    ) -> None:
        batch, _ = reconciled
        assert batch.status is BatchStatus.COMPLETED
        actions = set(
            db.execute(select(AuditEvent.action).where(AuditEvent.batch_id == batch.id)).scalars()
        )
        assert AuditAction.RUN_STARTED in actions
        assert AuditAction.RUN_COMPLETED in actions

    def test_audit_sequence_is_contiguous(
        self, db: Session, reconciled: tuple[ReconciliationBatch, object]
    ) -> None:
        """An append-only log must have no gaps, or a replay is not a replay."""
        batch, _ = reconciled
        sequences = sorted(
            db.execute(select(AuditEvent.sequence).where(AuditEvent.batch_id == batch.id)).scalars()
        )
        assert sequences == list(range(1, len(sequences) + 1))


class TestDeterminism:
    def test_same_seed_produces_identical_metrics(
        self, db: Session, settings: Settings, tmp_path: Path
    ) -> None:
        """Two runs of the same input must agree exactly.

        Without this, a reported metric is not reproducible and an audit replay
        proves nothing.
        """
        outcomes = []
        for index in range(2):
            batch, _ = materialize_dataset(
                db,
                DatasetSpec(name=f"determinism-{index}", seed=999, n_orders=250),
                tmp_path / f"run{index}",
                name=f"determinism-{index}",
            )
            result = ReconciliationPipeline(db, settings=settings).run(batch)
            outcomes.append(
                (
                    result.metrics.auto_accepted,
                    result.metrics.sent_to_review,
                    result.metrics.rejected_by_invariant,
                    result.metrics.unexplained_subunits,
                    result.metrics.balanced_settlements,
                )
            )
        assert outcomes[0] == outcomes[1]


class TestIngestionSafety:
    def test_identical_reupload_is_ignored(
        self, db: Session, settings: Settings, tmp_path: Path
    ) -> None:
        """A double-click must not double-count the ledger."""
        directory = tmp_path / "dup"
        write_dataset(DatasetSpec(name="dup", seed=77, n_orders=120), directory)
        batch = ReconciliationBatch(name="dup", status=BatchStatus.READY, currency="INR")
        db.add(batch)
        db.flush()

        path = directory / f"{SourceKind.RAZORPAY_PAYMENTS.value}.csv"
        payload = path.read_bytes()
        first = ingest_file(
            db,
            batch=batch,
            source_kind=SourceKind.RAZORPAY_PAYMENTS,
            filename=path.name,
            payload=payload,
        )
        second = ingest_file(
            db,
            batch=batch,
            source_kind=SourceKind.RAZORPAY_PAYMENTS,
            filename=path.name,
            payload=payload,
        )
        assert first.accepted > 0
        assert second.accepted == 0
        assert "duplicate_file_ignored" in second.warnings

        stored = db.execute(
            select(func.count()).select_from(SourceRecord).where(SourceRecord.batch_id == batch.id)
        ).scalar_one()
        assert stored == first.accepted

    def test_replayed_webhook_collapses(
        self, db: Session, settings: Settings, tmp_path: Path
    ) -> None:
        """Duplicate deliveries share a natural key and must merge, not stack."""
        directory = tmp_path / "events"
        write_dataset(
            DatasetSpec(name="events", seed=31, n_orders=400, duplicate_event_rate=0.5),
            directory,
        )
        batch = ReconciliationBatch(name="events", status=BatchStatus.READY, currency="INR")
        db.add(batch)
        db.flush()
        path = directory / f"{SourceKind.WEBHOOK_EVENTS.value}.csv"
        result = ingest_file(
            db,
            batch=batch,
            source_kind=SourceKind.WEBHOOK_EVENTS,
            filename=path.name,
            payload=path.read_bytes(),
        )
        assert result.duplicates_collapsed > 0

    def test_missing_required_column_rejects_whole_file(
        self, db: Session, settings: Settings
    ) -> None:
        """A changed export format must fail closed, not import partially."""
        batch = ReconciliationBatch(name="bad", status=BatchStatus.READY, currency="INR")
        db.add(batch)
        db.flush()
        payload = b"some_column,another\n1,2\n"
        with pytest.raises(StructuralIngestError, match="missing required column"):
            ingest_file(
                db,
                batch=batch,
                source_kind=SourceKind.BANK_STATEMENT,
                filename="broken.csv",
                payload=payload,
            )

    def test_high_row_error_rate_rejects_whole_file(self, db: Session, settings: Settings) -> None:
        batch = ReconciliationBatch(name="garbage", status=BatchStatus.READY, currency="INR")
        db.add(batch)
        db.flush()
        rows = "\n".join(
            f"txn{index},not-a-date,narration,ref,not-an-amount,INR,0" for index in range(50)
        )
        payload = (
            "Txn Id,Value Date,Narration,Chq/Ref No,Credit,Ccy,Closing Balance\n" + rows
        ).encode()
        with pytest.raises(StructuralIngestError, match="structurally invalid"):
            ingest_file(
                db,
                batch=batch,
                source_kind=SourceKind.BANK_STATEMENT,
                filename="garbage.csv",
                payload=payload,
            )

    def test_empty_file_rejected(self, db: Session, settings: Settings) -> None:
        batch = ReconciliationBatch(name="empty", status=BatchStatus.READY, currency="INR")
        db.add(batch)
        db.flush()
        with pytest.raises(StructuralIngestError):
            ingest_file(
                db,
                batch=batch,
                source_kind=SourceKind.BANK_STATEMENT,
                filename="empty.csv",
                payload=b"",
            )


class TestIngestionOrderIndependence:
    def test_ingestion_order_does_not_affect_the_result(
        self, db: Session, settings: Settings, tmp_path: Path
    ) -> None:
        """A payment referencing a not-yet-uploaded settlement must still resolve.

        Nothing about the pipeline should assume sources arrive in a particular
        sequence — a settlement export landing before the payments it settles,
        or a refund file uploaded before the payment it refunds, is routine, not
        exceptional. Two batches built from the same records in opposite file
        orders must reconcile identically.
        """
        directory = tmp_path / "order"
        write_dataset(DatasetSpec(name="order", seed=606, n_orders=250), directory)
        kinds = [kind for kind in SourceKind if (directory / f"{kind.value}.csv").exists()]
        assert len(kinds) >= 2, "the fixture needs at least two source files to prove order"

        outcomes = []
        for label, ordered_kinds in (("forward", kinds), ("reverse", list(reversed(kinds)))):
            batch = ReconciliationBatch(
                name=f"order-{label}", status=BatchStatus.READY, currency="INR"
            )
            db.add(batch)
            db.flush()
            for kind in ordered_kinds:
                path = directory / f"{kind.value}.csv"
                ingest_file(
                    db,
                    batch=batch,
                    source_kind=kind,
                    filename=path.name,
                    payload=path.read_bytes(),
                )
            result = ReconciliationPipeline(db, settings=settings).run(batch)
            db.commit()
            outcomes.append(
                (
                    result.metrics.total_records,
                    result.metrics.exact_links,
                    result.metrics.auto_accepted,
                    result.metrics.sent_to_review,
                    result.metrics.rejected_by_invariant,
                    result.metrics.unexplained_subunits,
                    result.metrics.balanced_settlements,
                )
            )
        assert outcomes[0] == outcomes[1]


class TestDriftRestriction:
    """The drift monitor only *flags* a batch; a later run is what must act on it."""

    def test_restriction_carries_into_the_next_batch_only(
        self, db: Session, settings: Settings, tmp_path: Path
    ) -> None:
        earlier, _ = materialize_dataset(
            db, DatasetSpec(name="drift-a", seed=701, n_orders=120), tmp_path / "drift-a"
        )
        ReconciliationPipeline(db, settings=settings).run(earlier)
        # Simulate what POST /batches/{id}/analyze would have set on `earlier`
        # after detecting drift against whatever came before it. The detector
        # itself (population_stability_index) is exercised separately; this
        # test is about what a later run does with the flag once it is set.
        earlier.automation_restricted = True
        earlier.restriction_reason = "test: simulated drift"
        db.commit()

        later, _ = materialize_dataset(
            db, DatasetSpec(name="drift-b", seed=702, n_orders=120), tmp_path / "drift-b"
        )
        pipeline = ReconciliationPipeline(db, settings=settings)
        default_max_risk = pipeline.policy.effective_max_risk
        result = pipeline.run(later)
        db.commit()

        assert pipeline.policy.effective_max_risk < default_max_risk
        assert later.automation_restricted is True
        assert later.restriction_reason is not None
        assert result.metrics.automation_restricted is True

        carried_events = list(
            db.execute(
                select(AuditEvent).where(
                    AuditEvent.batch_id == later.id,
                    AuditEvent.action == AuditAction.AUTOMATION_RESTRICTED,
                )
            ).scalars()
        )
        assert len(carried_events) == 1
        assert carried_events[0].detail is not None
        assert carried_events[0].detail.get("carried_from_batch_id") == earlier.id

    def test_an_unrestricted_batch_does_not_carry_a_restriction(
        self, db: Session, settings: Settings, tmp_path: Path
    ) -> None:
        clean, _ = materialize_dataset(
            db, DatasetSpec(name="clean-a", seed=703, n_orders=120), tmp_path / "clean-a"
        )
        ReconciliationPipeline(db, settings=settings).run(clean)
        db.commit()
        assert clean.automation_restricted is False

        later, _ = materialize_dataset(
            db, DatasetSpec(name="clean-b", seed=704, n_orders=120), tmp_path / "clean-b"
        )
        pipeline = ReconciliationPipeline(db, settings=settings)
        default_max_risk = pipeline.policy.effective_max_risk
        result = pipeline.run(later)
        db.commit()

        assert pipeline.policy.effective_max_risk == default_max_risk
        assert later.automation_restricted is False
        assert result.metrics.automation_restricted is False

    def test_apply_drift_restriction_is_a_no_op_with_no_prior_batch(
        self, db: Session, settings: Settings
    ) -> None:
        only = ReconciliationBatch(name="only", status=BatchStatus.READY, currency="INR")
        db.add(only)
        db.flush()
        pipeline = ReconciliationPipeline(db, settings=settings)
        default_max_risk = pipeline.policy.effective_max_risk
        metrics = ReconciliationMetrics()
        pipeline._apply_drift_restriction(only, metrics)
        assert pipeline.policy.effective_max_risk == default_max_risk
        assert metrics.automation_restricted is False


class TestPipelineFailureIsAtomic:
    """A crash partway through run() must leave the database exactly as it was.

    `run()` itself never commits; `POST /batches/{id}/run` owns the
    transaction and rolls back on any exception before marking the batch
    FAILED (see `api/routes/batches.py::run_batch`). That rollback is the
    actual atomicity guarantee, and it has never been exercised by a test
    that forces a failure after real writes have already happened — the
    existing structural-ingestion tests only prove atomic rejection at the
    file-parsing stage, before the pipeline runs at all.
    """

    def test_a_failure_after_persistence_leaves_no_partial_results(
        self,
        db: Session,
        settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        batch, _truth = materialize_dataset(
            db, DatasetSpec(name="atomic", seed=909, n_orders=200), tmp_path / "atomic"
        )
        # Commit a known-good baseline so the rollback below has a durable,
        # disk-backed state to return to, not just an uncommitted flush.
        db.commit()
        assert batch.status is BatchStatus.READY
        assert batch.started_at is None

        # _create_exceptions is the last stage `run()` calls, after `_persist`
        # has already flushed real MatchCandidate/ReconciliationMatch/
        # EvidenceItem/AccountingCheck rows for this batch. Failing here is
        # the strongest version of this test: those rows genuinely exist,
        # uncommitted, in the transaction at the moment of the crash. The
        # patch is deliberately signature-agnostic (`*args, **kwargs`) rather
        # than typed against `_create_exceptions`'s params, since those are
        # private and not part of any contract this test should couple to.
        def _boom(self: ReconciliationPipeline, *args: object, **kwargs: object) -> None:
            raise RuntimeError("injected failure: exception creation crashed")

        monkeypatch.setattr(ReconciliationPipeline, "_create_exceptions", _boom)

        pipeline = ReconciliationPipeline(db, settings=settings)
        with pytest.raises(RuntimeError, match="injected failure"):
            pipeline.run(batch)

        # Prove the crash happened after real writes, not before them.
        matches_before_rollback = db.execute(
            select(func.count())
            .select_from(ReconciliationMatch)
            .where(ReconciliationMatch.batch_id == batch.id)
        ).scalar_one()
        assert matches_before_rollback > 0

        # This is exactly what POST /batches/{id}/run does on any exception.
        db.rollback()

        for model in (
            MatchCandidate,
            ReconciliationMatch,
            EvidenceItem,
            AccountingCheck,
            ReconciliationException,
        ):
            count = db.execute(
                select(func.count()).select_from(model).where(model.batch_id == batch.id)
            ).scalar_one()
            assert count == 0, f"{model.__name__} rows survived a rolled-back run"

        assert batch.status is BatchStatus.READY
        assert batch.started_at is None


class TestNormalization:
    def test_messy_amounts_and_dates_survive_ingestion(
        self, db: Session, settings: Settings, tmp_path: Path
    ) -> None:
        batch, _ = materialize_dataset(
            db, DatasetSpec(name="norm", seed=8, n_orders=200), tmp_path / "norm"
        )
        records = list(
            db.execute(
                select(SourceRecord).where(
                    SourceRecord.batch_id == batch.id,
                    SourceRecord.record_kind == RecordKind.BANK_CREDIT,
                )
            ).scalars()
        )
        assert records
        for record in records:
            assert record.amount_subunits > 0
            assert record.currency == "INR"
            # Every bank reference must normalize to a comparable form, since
            # that value is the join key for UTR matching.
            if record.bank_ref:
                assert record.bank_ref_normalized
                assert record.bank_ref_normalized.islower()
