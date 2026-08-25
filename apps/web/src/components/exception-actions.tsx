"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { Badge, Card, CardBody, CardHeader } from "@/components/ui";
import { API_BASE, type CandidateSummary, type ExceptionDetail } from "@/lib/api";
import { money } from "@/lib/format";

/**
 * Reviewer controls.
 *
 * Approving a recommendation and choosing a different candidate are separate
 * actions on purpose. The backend records the second as an override, which is
 * the honest measure of how often the agent is wrong; folding them into one
 * button would erase that signal.
 */
export function ExceptionActions({
  exception,
  recommendedCandidateId,
}: {
  exception: ExceptionDetail;
  recommendedCandidateId: string | null;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState("finance.reviewer");
  const [notes, setNotes] = useState("");
  const [selected, setSelected] = useState<string | null>(recommendedCandidateId);

  const resolved = exception.status === "resolved" || exception.status === "written_off";

  async function submit(action: string, candidateId?: string | null) {
    setBusy(action);
    setError(null);
    try {
      const response = await fetch(
        `${API_BASE}/api/exceptions/${exception.id}/resolve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action,
            reviewer: reviewer.trim() || "reviewer",
            candidate_id: candidateId ?? undefined,
            notes: notes.trim() || undefined,
          }),
        },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail ?? `${response.status} ${response.statusText}`);
      }
      startTransition(() => router.refresh());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setBusy(null);
    }
  }

  async function investigate() {
    setBusy("investigate");
    setError(null);
    try {
      const response = await fetch(
        `${API_BASE}/api/agent/investigate/${exception.id}`,
        { method: "POST" },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail ?? `${response.status} ${response.statusText}`);
      }
      startTransition(() => router.refresh());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setBusy(null);
    }
  }

  const working = busy !== null || pending;

  if (resolved) {
    return (
      <Card>
        <CardHeader title="Resolved" />
        <CardBody className="flex flex-col gap-2">
          <Badge tone="proven">
            {exception.resolution?.action === "write_off"
              ? "Written off"
              : "Match posted"}
          </Badge>
          {exception.resolution ? (
            <div className="text-xs leading-relaxed text-ink-2">
              {exception.resolution.reviewer} chose{" "}
              {exception.resolution.action.replace(/_/g, " ")}
              {exception.resolution.overrode_agent
                ? ", overriding the agent's recommendation"
                : ""}
              .
              {exception.resolution.notes ? ` “${exception.resolution.notes}”` : ""}
            </div>
          ) : null}
        </CardBody>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader
        title="Reviewer decision"
        description="Only a human can post a match the automatic layer declined."
      />
      <CardBody className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="reviewer"
            className="text-[11px] font-medium uppercase tracking-wider text-ink-3"
          >
            Reviewer
          </label>
          <input
            id="reviewer"
            value={reviewer}
            onChange={(event) => setReviewer(event.target.value)}
            className="rounded-md border border-line bg-surface-2 px-3 py-2 text-xs text-ink outline-none focus:border-accent"
            placeholder="your.name"
          />
        </div>

        {exception.candidates.length > 0 ? (
          <fieldset className="flex flex-col gap-1.5">
            <legend className="mb-1 text-[11px] font-medium uppercase tracking-wider text-ink-3">
              Candidate to post
            </legend>
            {exception.candidates.map((candidate) => (
              <CandidateOption
                key={candidate.id}
                candidate={candidate}
                selected={selected === candidate.id}
                recommended={candidate.id === recommendedCandidateId}
                onSelect={() => setSelected(candidate.id)}
              />
            ))}
          </fieldset>
        ) : (
          <div className="rounded-md border border-line bg-surface-2/50 px-3 py-2 text-xs leading-relaxed text-ink-3">
            No candidate link exists for this record, so there is nothing to post. It
            can be left open or written off.
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="notes"
            className="text-[11px] font-medium uppercase tracking-wider text-ink-3"
          >
            Notes
          </label>
          <textarea
            id="notes"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            rows={2}
            className="resize-y rounded-md border border-line bg-surface-2 px-3 py-2 text-xs text-ink outline-none focus:border-accent"
            placeholder="What did you check?"
          />
        </div>

        {error ? (
          <div className="rounded-md border border-blocked/30 bg-blocked-soft/25 px-3 py-2 text-xs text-blocked">
            {error}
          </div>
        ) : null}

        <div className="flex flex-wrap gap-2">
          {recommendedCandidateId ? (
            <button
              type="button"
              disabled={working}
              onClick={() => submit("approve", recommendedCandidateId)}
              className="rounded-md border border-proven/40 bg-proven-soft/50 px-3 py-2 text-xs font-semibold text-proven transition-colors hover:bg-proven-soft disabled:opacity-50"
            >
              {busy === "approve" ? "Approving…" : "Approve recommendation"}
            </button>
          ) : null}
          {selected && selected !== recommendedCandidateId ? (
            <button
              type="button"
              disabled={working}
              onClick={() => submit("choose", selected)}
              className="rounded-md border border-accent/40 bg-accent-soft/50 px-3 py-2 text-xs font-semibold text-accent transition-colors hover:bg-accent-soft disabled:opacity-50"
            >
              {busy === "choose" ? "Posting…" : "Post selected candidate"}
            </button>
          ) : null}
          {!exception.agent_run_id ? (
            <button
              type="button"
              disabled={working}
              onClick={investigate}
              className="rounded-md border border-line-strong bg-surface-2 px-3 py-2 text-xs font-semibold text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink disabled:opacity-50"
            >
              {busy === "investigate" ? "Investigating…" : "Run investigation"}
            </button>
          ) : (
            <button
              type="button"
              disabled={working}
              onClick={investigate}
              className="rounded-md border border-line-strong bg-surface-2 px-3 py-2 text-xs font-semibold text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink disabled:opacity-50"
            >
              {busy === "investigate" ? "Investigating…" : "Re-investigate"}
            </button>
          )}
          <button
            type="button"
            disabled={working}
            onClick={() => submit("write_off")}
            className="rounded-md border border-line bg-transparent px-3 py-2 text-xs font-medium text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink-2 disabled:opacity-50"
          >
            {busy === "write_off" ? "Writing off…" : "Write off"}
          </button>
        </div>

        <p className="text-[11px] leading-relaxed text-ink-3">
          Choosing a candidate the agent did not recommend is recorded as an override,
          and both the choice and the rejected alternatives become labelled training
          examples.
        </p>
      </CardBody>
    </Card>
  );
}

function CandidateOption({
  candidate,
  selected,
  recommended,
  onSelect,
}: {
  candidate: CandidateSummary;
  selected: boolean;
  recommended: boolean;
  onSelect: () => void;
}) {
  const blocking = candidate.invariants.filter((check) => !check.passed);
  return (
    <label
      className={`flex cursor-pointer gap-3 rounded-lg border px-3 py-2.5 transition-colors ${
        selected
          ? "border-accent/50 bg-accent-soft/25"
          : "border-line bg-surface-2/40 hover:bg-surface-2"
      }`}
    >
      <input
        type="radio"
        name="candidate"
        checked={selected}
        onChange={onSelect}
        className="mt-1 accent-[oklch(0.72_0.14_235)]"
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs font-medium text-ink">
            {candidate.relation.replace(/_/g, " ")}
          </span>
          {recommended ? <Badge tone="accent">Agent pick</Badge> : null}
          {candidate.score !== null ? (
            <Badge tone="neutral">score {candidate.score.toFixed(3)}</Badge>
          ) : null}
          {blocking.length > 0 ? (
            <Badge tone="blocked">{blocking.length} check failed</Badge>
          ) : null}
        </div>
        <div className="tabular mt-1 text-[11px] text-ink-3">
          {money(candidate.left.amount)} ·{" "}
          {candidate.right.reference ?? candidate.right.id.slice(0, 10)}
        </div>
      </div>
    </label>
  );
}
