"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Callout, Card, CardBody, CardHeader, Mono } from "@/components/ui";
import { API_BASE } from "@/lib/api";

const SOURCES: Array<{ kind: string; label: string; hint: string }> = [
  { kind: "order_ledger", label: "Order ledger", hint: "Merchant order export" },
  { kind: "razorpay_payments", label: "Payments", hint: "Razorpay payments export" },
  { kind: "razorpay_refunds", label: "Refunds", hint: "Razorpay refunds export" },
  { kind: "settlement_report", label: "Settlements", hint: "Settlement report" },
  { kind: "fee_report", label: "Fees", hint: "Fee report — the payment↔settlement bridge" },
  { kind: "bank_statement", label: "Bank statement", hint: "Credits as the bank recorded them" },
  { kind: "webhook_events", label: "Webhooks", hint: "Payment events; duplicates collapse" },
];

type UploadResult = {
  kind: string;
  filename: string;
  accepted_rows?: number;
  rejected_rows?: number;
  error?: string;
};

/**
 * Create a draft batch, attach CSVs, optionally run reconciliation.
 *
 * Files are posted one at a time so a structural fault names the file that
 * caused it instead of failing the whole request with no attribution.
 */
export function IngestForm() {
  const router = useRouter();
  const [name, setName] = useState("Manual ingest");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<UploadResult[]>([]);
  const [files, setFiles] = useState<Record<string, File | null>>(
    Object.fromEntries(SOURCES.map((source) => [source.kind, null])),
  );

  async function submit(runAfter: boolean) {
    setBusy(true);
    setError(null);
    setResults([]);
    try {
      const created = await fetch(
        `${API_BASE}/api/batches?${new URLSearchParams({ name: name.trim() || "Manual ingest", currency: "INR" })}`,
        { method: "POST" },
      );
      if (!created.ok) {
        const payload = await created.json().catch(() => null);
        throw new Error(payload?.detail ?? "Could not create batch");
      }
      const batch = (await created.json()) as { id: string };
      const outcomes: UploadResult[] = [];
      for (const source of SOURCES) {
        const file = files[source.kind];
        if (!file) continue;
        const body = new FormData();
        body.append("file", file);
        const uploaded = await fetch(
          `${API_BASE}/api/batches/${batch.id}/sources?${new URLSearchParams({ source_kind: source.kind })}`,
          { method: "POST", body },
        );
        const payload = await uploaded.json().catch(() => null);
        if (!uploaded.ok) {
          outcomes.push({
            kind: source.kind,
            filename: file.name,
            error:
              typeof payload?.detail === "string"
                ? payload.detail
                : `Upload failed (${uploaded.status})`,
          });
          continue;
        }
        outcomes.push({
          kind: source.kind,
          filename: file.name,
          accepted_rows: payload.accepted_rows,
          rejected_rows: payload.rejected_rows,
        });
      }
      setResults(outcomes);
      const fatal = outcomes.some((item) => item.error);
      if (fatal) {
        setError(
          "At least one file was rejected. The batch is still a draft; fix the file and try again.",
        );
        return;
      }
      if (outcomes.length === 0) {
        setError("Attach at least one CSV. An empty batch has nothing to reconcile.");
        return;
      }
      if (runAfter) {
        const run = await fetch(`${API_BASE}/api/batches/${batch.id}/run`, {
          method: "POST",
        });
        if (!run.ok) {
          const payload = await run.json().catch(() => null);
          throw new Error(payload?.detail ?? "Reconciliation failed");
        }
      }
      router.push(`/batches/${batch.id}`);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Ingest failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader
        title="Source files"
        description="CSV only. Column mapping is inferred; pass an unrecognised layout and the file is rejected rather than silently reinterpreted."
      />
      <CardBody className="flex flex-col gap-5">
        <label className="flex flex-col gap-1 text-xs text-ink-3 sm:max-w-md">
          Batch name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            className="rounded-md border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-accent"
          />
        </label>
        <ul className="flex flex-col gap-3">
          {SOURCES.map((source) => (
            <li
              key={source.kind}
              className="flex flex-col gap-1 rounded-lg border border-line bg-surface-2/40 px-3 py-3 sm:flex-row sm:items-center sm:justify-between"
            >
              <div>
                <div className="text-xs font-medium text-ink">{source.label}</div>
                <div className="text-[11px] text-ink-3">{source.hint}</div>
              </div>
              <label className="mt-2 text-[11px] text-ink-3 sm:mt-0">
                <span className="sr-only">{source.label} file</span>
                <input
                  type="file"
                  accept=".csv,text/csv"
                  onChange={(event) =>
                    setFiles((current) => ({
                      ...current,
                      [source.kind]: event.target.files?.[0] ?? null,
                    }))
                  }
                  className="max-w-[240px] text-[11px] text-ink-2 file:mr-2 file:rounded-md file:border file:border-line-strong file:bg-surface-2 file:px-2 file:py-1 file:text-[11px] file:text-ink-2"
                />
              </label>
            </li>
          ))}
        </ul>
        {results.length > 0 ? (
          <ul className="flex flex-col gap-1.5 text-xs">
            {results.map((result) => (
              <li key={result.kind} className={result.error ? "text-blocked" : "text-ink-2"}>
                <Mono>{result.filename}</Mono>
                {result.error
                  ? ` — ${result.error}`
                  : ` — ${result.accepted_rows} accepted, ${result.rejected_rows ?? 0} rejected`}
              </li>
            ))}
          </ul>
        ) : null}
        {error ? (
          <Callout tone="blocked" title="Ingest stopped">
            {error}
          </Callout>
        ) : (
          <Callout tone="neutral">
            Prefer <Mono>make demo</Mono> if you want the seeded hold-out numbers
            from the README. This form is for exercising the real ingest path with
            your own CSVs.
          </Callout>
        )}
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => submit(false)}
            className="rounded-md border border-line-strong bg-surface-2 px-3 py-2 text-xs font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink disabled:opacity-50"
          >
            {busy ? "Working…" : "Upload without reconciling"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => submit(true)}
            className="rounded-md border border-accent/40 bg-accent-soft/40 px-3 py-2 text-xs font-semibold text-accent transition-colors hover:bg-accent-soft disabled:opacity-50"
          >
            {busy ? "Working…" : "Upload and reconcile"}
          </button>
        </div>
      </CardBody>
    </Card>
  );
}
