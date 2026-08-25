"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { Badge, Card, CardBody, CardHeader, Mono, Table, Td, Th } from "@/components/ui";
import { API_BASE, type ModelVersionSummary } from "@/lib/api";
import { percent, titleize } from "@/lib/format";

/**
 * Promotion UI.
 *
 * Evaluate and promote are separate actions because passing the quantitative
 * gates only makes a model *eligible*. A named human still has to accept it.
 * Collapsing those into one button would hide the fact that metrics alone
 * cannot change what the system is allowed to automate.
 */
export function ModelRegistry({ models }: { models: ModelVersionSummary[] }) {
  const router = useRouter();
  const [approver, setApprover] = useState("finance.controller");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const sorted = useMemo(
    () =>
      [...models].sort((a, b) => {
        if (a.stage === "production" && b.stage !== "production") return -1;
        if (b.stage === "production" && a.stage !== "production") return 1;
        return a.name.localeCompare(b.name);
      }),
    [models],
  );

  async function evaluate(id: string) {
    setBusy(`evaluate:${id}`);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(`${API_BASE}/api/models/${id}/evaluate`, {
        method: "POST",
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(payload?.detail ?? `${response.status} ${response.statusText}`);
      }
      const failures = (payload.failures as string[] | undefined) ?? [];
      setNotice(
        payload.passed_gates
          ? "Gates passed. Promotion still needs a named approver."
          : `Gates failed: ${failures.join("; ") || "see details"}`,
      );
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Evaluation failed");
    } finally {
      setBusy(null);
    }
  }

  async function promote(id: string) {
    const name = approver.trim();
    if (!name) {
      setError("Promotion requires a named approver. The system cannot sign itself off.");
      return;
    }
    setBusy(`promote:${id}`);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(
        `${API_BASE}/api/models/${id}/promote?${new URLSearchParams({ approved_by: name })}`,
        { method: "POST" },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = payload?.detail;
        throw new Error(
          typeof detail === "string" ? detail : `${response.status} ${response.statusText}`,
        );
      }
      setNotice(`${name} promoted ${payload.name} to production. The previous incumbent is retired.`);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Promotion failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <CardHeader
        title="Model registry"
        description="A challenger must pass the policy gates and be approved by a named human. Metrics cannot promote themselves."
      />
      {sorted.length === 0 ? (
        <CardBody>
          <p className="text-xs leading-relaxed text-ink-3">
            No model versions are registered yet. <Mono>make demo</Mono> fits a
            scorer, records its hold-out evaluation, and leaves it in production
            so these controls have something to act on.
          </p>
        </CardBody>
      ) : (
        <>
          <Table>
            <thead>
              <tr>
                <Th>Model</Th>
                <Th>Stage</Th>
                <Th align="right">Precision bound</Th>
                <Th align="right">Coverage</Th>
                <Th>Gates</Th>
                <Th>Actions</Th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((model) => (
                <tr key={model.id}>
                  <Td>
                    <div className="font-medium text-ink">{model.name}</div>
                    <div className="mt-0.5 text-[11px] text-ink-3">
                      {titleize(model.kind)}
                      {model.promoted_by
                        ? ` · promoted by ${model.promoted_by}`
                        : ""}
                    </div>
                  </Td>
                  <Td>
                    <Badge
                      tone={
                        model.stage === "production"
                          ? "proven"
                          : model.stage === "retired"
                            ? "neutral"
                            : "review"
                      }
                    >
                      {titleize(model.stage)}
                    </Badge>
                  </Td>
                  <Td align="right" className="tabular">
                    {model.precision_lower_bound == null
                      ? "—"
                      : percent(model.precision_lower_bound, 2)}
                  </Td>
                  <Td align="right" className="tabular">
                    {model.coverage == null ? "—" : percent(model.coverage, 1)}
                  </Td>
                  <Td>
                    {model.gates_passed == null ? (
                      <span className="text-xs text-ink-3">Not evaluated</span>
                    ) : model.gates_passed ? (
                      <Badge tone="proven">Eligible</Badge>
                    ) : (
                      <Badge tone="blocked" title={model.gate_failures.join("; ")}>
                        Blocked
                      </Badge>
                    )}
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1.5">
                      <button
                        type="button"
                        disabled={busy !== null}
                        onClick={() => evaluate(model.id)}
                        className="rounded-md border border-line-strong bg-surface-2 px-2.5 py-1 text-[11px] font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink disabled:opacity-50"
                      >
                        {busy === `evaluate:${model.id}` ? "Checking…" : "Evaluate gates"}
                      </button>
                      {model.stage !== "production" ? (
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => promote(model.id)}
                          className="rounded-md border border-proven/40 bg-proven-soft/40 px-2.5 py-1 text-[11px] font-semibold text-proven transition-colors hover:bg-proven-soft disabled:opacity-50"
                        >
                          {busy === `promote:${model.id}` ? "Promoting…" : "Promote"}
                        </button>
                      ) : null}
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <CardBody className="flex flex-col gap-3 border-t border-line">
            <label className="flex flex-col gap-1 text-xs text-ink-3 sm:max-w-sm">
              Approver
              <input
                value={approver}
                onChange={(event) => setApprover(event.target.value)}
                className="rounded-md border border-line bg-canvas px-3 py-1.5 text-xs text-ink outline-none focus:border-accent"
                autoComplete="off"
              />
            </label>
            {error ? (
              <div className="rounded-md border border-blocked/30 bg-blocked-soft/25 px-3 py-2 text-xs text-blocked">
                {error}
              </div>
            ) : null}
            {notice ? (
              <div className="rounded-md border border-proven/30 bg-proven-soft/25 px-3 py-2 text-xs text-ink-2">
                {notice}
              </div>
            ) : null}
          </CardBody>
        </>
      )}
    </Card>
  );
}
