import Link from "next/link";

import { Callout, Card, CardBody, CardHeader } from "@/components/ui";

export default function ArchitecturePage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          How ReconProof decides
        </h1>
        <p className="max-w-3xl text-sm leading-relaxed text-ink-2">
          Each stage handles only what the previous one could not prove. The last
          word is a deterministic check, not a model score. The investigation
          agent proposes; it never posts.
        </p>
      </div>

      <Card>
        <CardHeader title="Pipeline order" />
        <CardBody>
          <ol className="flex flex-col gap-3 text-sm leading-relaxed text-ink-2">
            <Step n="1" title="Validate">
              A structural fault — missing column, unreadable header, error rate
              above 5% — rejects the file inside a transaction. An isolated bad
              row is recorded with its amount so the rupee stays attributable.
            </Step>
            <Step n="2" title="Normalize">
              Integer paise, comparable references. Original cells are kept.
              Assumptions (UTC, day-first, date-only) are stored on the record.
            </Step>
            <Step n="3" title="Exact identifiers">
              Shared ids are proof. The fee report is the only file with both a
              payment id and a settlement id, so it bridges a link neither file
              states alone.
            </Step>
            <Step n="4" title="Subset-sum refunds">
              A settlement reports a refund total, not which refunds. Pairwise
              scoring of that relation was 18% precision; exact subset-sum
              collapses it to arithmetic.
            </Step>
            <Step n="5" title="Calibrated scoring">
              Remaining pairs get a logistic score. The threshold is the lowest
              value whose Wilson lower bound on precision still clears 99%.
              Thin evidence turns automation off rather than guessing.
            </Step>
            <Step n="6" title="Global assignment">
              Competing claims on the same record are resolved as an assignment
              problem. Capacity is per (record, relation), so a payment can
              refund and settle.
            </Step>
            <Step n="7" title="Invariants, then the queue">
              Pairwise and settlement-balance checks veto financially impossible
              links. Everything unproven goes to the exception queue; unexplained
              rupees must equal queue totals.
            </Step>
            <Step n="8" title="Bounded agent, human sign-off">
              Nine read-only tools, a hard budget, a verifier that recomputes
              allocations and citations. Drift can only tighten automation.
            </Step>
          </ol>
        </CardBody>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="Where AI is used — and where it is not" />
          <CardBody className="flex flex-col gap-2 text-xs leading-relaxed text-ink-2">
            <p>
              Identifiers, subset-sum, ledger arithmetic, invariant checks, and
              the promotion gates are deterministic. Logistic regression is used
              where the correspondence is genuinely uncertain. The agent is used
              where the evidence is unstructured. A language model is optional;
              the default provider is a rule engine that still walks all seven
              investigation phases.
            </p>
            <Callout tone="neutral">
              The committee asked for judgement about where not to use AI. This
              is that list, implemented as code paths that do not call a model.
            </Callout>
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="What a reviewer should open" />
          <CardBody className="flex flex-col gap-2 text-xs leading-relaxed text-ink-2">
            <Link className="text-accent hover:underline" href="/">
              Dashboard — held-out run metrics
            </Link>
            <Link className="text-accent hover:underline" href="/models">
              Model &amp; policy — thresholds, coefficients, promotion
            </Link>
            <Link className="text-accent hover:underline" href="/ingest">
              Ingest — CSV upload without the demo generator
            </Link>
            <p className="pt-2 text-ink-3">
              Docs in the repo: <code>docs/architecture.md</code>,{" "}
              <code>docs/model-card.md</code>, <code>docs/threat-model.md</code>,{" "}
              <code>docs/pitch.md</code>.
            </p>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

function Step({
  n,
  title,
  children,
}: {
  n: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <li className="flex gap-3">
      <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-line bg-surface-2 text-[11px] font-semibold text-ink-2">
        {n}
      </span>
      <div>
        <div className="font-medium text-ink">{title}</div>
        <p className="mt-0.5 text-xs text-ink-3">{children}</p>
      </div>
    </li>
  );
}
