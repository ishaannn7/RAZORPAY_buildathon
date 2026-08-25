"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { API_BASE, type RazorpaySyncResult } from "@/lib/api";

function defaultWindow(): { from: string; to: string } {
  const to = new Date();
  const from = new Date(to.getTime() - 7 * 24 * 60 * 60 * 1000);
  // `datetime-local` inputs want "YYYY-MM-DDTHH:mm" in the viewer's own
  // timezone; converted to a UTC ISO string only when the request is sent.
  const local = (date: Date) => {
    const offset = date.getTimezoneOffset() * 60_000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 16);
  };
  return { from: local(from), to: local(to) };
}

/**
 * Pull payments and refunds for a window straight from Razorpay's API,
 * equivalent to uploading a `razorpay_payments` + `razorpay_refunds` export
 * for the same window. Additive to the file-upload path on the ingest page,
 * not a replacement for it — a merchant without Razorpay credentials never
 * sees this fail, they just don't have a reason to open it.
 */
export function RazorpaySync({ batchId }: { batchId: string }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RazorpaySyncResult | null>(null);
  const [window, setWindow] = useState(defaultWindow);

  async function sync() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const response = await fetch(`${API_BASE}/api/integrations/razorpay/sync`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          batch_id: batchId,
          from_time: new Date(window.from).toISOString(),
          to_time: new Date(window.to).toISOString(),
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(
          typeof payload?.detail === "string"
            ? payload.detail
            : `${response.status} ${response.statusText}`,
        );
      }
      setResult(payload as RazorpaySyncResult);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sync failed");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-xs font-medium text-accent hover:underline"
      >
        Sync from Razorpay →
      </button>
    );
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-line bg-surface-2/40 px-3 py-3">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-[11px] text-ink-3">
          From
          <input
            type="datetime-local"
            value={window.from}
            onChange={(event) => setWindow((current) => ({ ...current, from: event.target.value }))}
            className="rounded-md border border-line bg-canvas px-2 py-1 text-xs text-ink outline-none focus:border-accent"
          />
        </label>
        <label className="flex flex-col gap-1 text-[11px] text-ink-3">
          To
          <input
            type="datetime-local"
            value={window.to}
            onChange={(event) => setWindow((current) => ({ ...current, to: event.target.value }))}
            className="rounded-md border border-line bg-canvas px-2 py-1 text-xs text-ink outline-none focus:border-accent"
          />
        </label>
        <button
          type="button"
          disabled={busy}
          onClick={sync}
          className="rounded-md border border-accent/40 bg-accent-soft/40 px-3 py-1.5 text-xs font-semibold text-accent transition-colors hover:bg-accent-soft disabled:opacity-50"
        >
          {busy ? "Syncing…" : "Sync"}
        </button>
      </div>
      {error ? <span className="text-[11px] text-blocked">{error}</span> : null}
      {result ? (
        <span className="text-[11px] text-ink-2">
          {result.payments ? `${result.payments.accepted_rows} payment(s)` : "no payments"} ·{" "}
          {result.refunds ? `${result.refunds.accepted_rows} refund(s)` : "no refunds"} in window
        </span>
      ) : null}
      <span className="text-[11px] text-ink-3">
        Requires <code>RECONPROOF_RAZORPAY_KEY_ID</code> / <code>_KEY_SECRET</code> set on the
        API — a 503 here means they aren&apos;t configured.
      </span>
    </div>
  );
}
