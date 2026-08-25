"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { API_BASE } from "@/lib/api";

export function RunBatchButton({
  batchId,
  status,
}: {
  batchId: string;
  status: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (status === "running") {
    return <span className="text-xs text-ink-3">Reconciliation in progress…</span>;
  }
  if (status === "completed") return null;

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/api/batches/${batchId}/run`, {
        method: "POST",
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(
          typeof payload?.detail === "string"
            ? payload.detail
            : `${response.status} ${response.statusText}`,
        );
      }
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Run failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        disabled={busy}
        onClick={run}
        className="rounded-md border border-accent/40 bg-accent-soft/40 px-3 py-1.5 text-xs font-semibold text-accent transition-colors hover:bg-accent-soft disabled:opacity-50"
      >
        {busy ? "Reconciling…" : status === "failed" ? "Retry reconciliation" : "Run reconciliation"}
      </button>
      {error ? <span className="max-w-xs text-[11px] text-blocked">{error}</span> : null}
    </div>
  );
}
