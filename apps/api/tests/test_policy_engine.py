"""The policy engine, tested in isolation from the pipeline that calls it.

There is no OPA sidecar in this architecture — `PolicyEngine` *is* the policy
engine, not a fallback for one, so there is no separate "policy service is
down" failure mode to simulate. The equivalent real failure is a policy
document that fails to load: a missing or malformed file must refuse to
produce an engine at all, rather than silently falling back to something
permissive. That is what "fail closed" means for this component, and it had
no dedicated test coverage before this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reconproof.policy.engine import DEFAULT_POLICY, PolicyEngine


class TestPolicyDocumentFailsClosed:
    """A policy engine that cannot prove its rules must not run any."""

    def test_missing_file_refuses_to_load(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.json"
        with pytest.raises(FileNotFoundError):
            PolicyEngine.from_file(missing)

    def test_malformed_json_refuses_to_load(self, tmp_path: Path) -> None:
        broken = tmp_path / "policy.json"
        broken.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            PolicyEngine.from_file(broken)

    def test_valid_file_round_trips_the_default_document(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.json"
        path.write_text(json.dumps(DEFAULT_POLICY), encoding="utf-8")
        engine = PolicyEngine.from_file(path)
        assert engine.name == DEFAULT_POLICY["name"]
        assert engine.version == DEFAULT_POLICY["version"]
        assert engine.digest() == PolicyEngine.default().digest()


class TestDriftTightening:
    """Tightening is the only direction a policy is allowed to move on its own."""

    def test_tighten_for_drift_reduces_the_effective_risk_budget(self) -> None:
        engine = PolicyEngine.default()
        base = engine.effective_max_risk
        engine.tighten_for_drift()
        assert engine.effective_max_risk < base

    def test_tighten_for_drift_uses_the_configured_factor_by_default(self) -> None:
        engine = PolicyEngine.default()
        base = engine.effective_max_risk
        configured = float(DEFAULT_POLICY["drift"]["risk_tightening_factor"])
        engine.tighten_for_drift()
        assert engine.effective_max_risk == pytest.approx(base * configured)

    def test_tighten_for_drift_accepts_an_explicit_factor(self) -> None:
        engine = PolicyEngine.default()
        base = engine.effective_max_risk
        engine.tighten_for_drift(factor=0.1)
        assert engine.effective_max_risk == pytest.approx(base * 0.1)

    def test_untightened_engine_uses_the_document_value_unchanged(self) -> None:
        engine = PolicyEngine.default()
        assert engine.effective_max_risk == DEFAULT_POLICY["automation"]["max_risk"]


class TestAgentBudget:
    def test_no_tool_name_grants_a_mutating_verb(self) -> None:
        """Defence in depth: even a corrupted allowlist should read as read-only."""
        engine = PolicyEngine.default()
        mutating = ("write", "post", "update", "delete", "execute", "modify")
        for tool in engine.agent_budget().allowed_tools:
            lowered = tool.lower()
            assert not any(verb in lowered for verb in mutating), tool

    def test_tool_allowed_matches_the_agent_budget(self) -> None:
        engine = PolicyEngine.default()
        allowed = engine.agent_budget().allowed_tools
        assert engine.tool_allowed(next(iter(allowed)))
        assert not engine.tool_allowed("execute_sql")


class TestModelPromotionGates:
    def test_weak_precision_bound_fails_the_gate(self) -> None:
        engine = PolicyEngine.default()
        passed, failures = engine.evaluate_promotion({"precision_lower_bound": 0.5}, None)
        assert passed is False
        assert failures

    def test_strong_bound_with_no_incumbent_passes(self) -> None:
        engine = PolicyEngine.default()
        passed, failures = engine.evaluate_promotion(
            {"precision_lower_bound": 0.999, "coverage": 0.8}, None
        )
        assert passed is True
        assert failures == []

    def test_regression_against_the_incumbent_fails_even_above_the_floor(self) -> None:
        engine = PolicyEngine.default()
        incumbent = {"precision_lower_bound": 0.995, "coverage": 0.8}
        passed, failures = engine.evaluate_promotion(
            {"precision_lower_bound": 0.991, "coverage": 0.8}, incumbent
        )
        assert passed is False
        assert any("regresses" in failure for failure in failures)
