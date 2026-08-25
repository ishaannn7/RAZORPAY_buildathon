import Link from "next/link";
import { notFound } from "next/navigation";

import { AgentTrace } from "@/components/agent-trace";
import { ExceptionActions } from "@/components/exception-actions";
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  Callout,
  Mono,
  StatusBadge,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api, type CandidateSummary, type RecordSummary } from "@/lib/api";
import {
  CATEGORY_LABELS,
  dateTime,
  money,
  RELATION_LABELS,
  shortId,
  titleize,
} from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function ExceptionReviewPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const exception = await api.exception(id);
  if (!exception) notFound();

  const run = exception.agent_run_id ? await api.agentRun(exception.agent_run_id) : null;
  const recommendation = run?.recommendation as { candidate_id?: string } | null;
  const recommendedCandidateId = recommendation?.candidate_id ?? null;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-3">
          <Link href="/" className="hover:text-ink-2">
            Batches
          </Link>
          <span aria-hidden>/</span>
          <Link
            href={`/batches/${exception.batch_id}/exceptions`}
            className="hover:text-ink-2"
          >
            Exception queue
          </Link>
          <span aria-hidden>/</span>
          <Mono>{shortId(exception.id, 10)}</Mono>
        </div>
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="tabular text-xl font-semibold tracking-tight text-ink">
            {money(exception.amount)}
          </h1>
          <Badge tone="review">
            {CATEGORY_LABELS[exception.category] ?? titleize(exception.category)}
          </Badge>
          <StatusBadge status={exception.status} />
        </div>
        <p className="max-w-3xl text-sm leading-relaxed text-ink-2">
          {exception.summary}
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex flex-col gap-6">
          <Card>
            <CardHeader
              title="Subject record"
              description="Shown exactly as ingested. Source values are never edited."
            />
            <CardBody>
              <RecordPanel record={exception.subject} />
            </CardBody>
          </Card>

          {exception.counterfactual &&
          exception.counterfactual.statements.length > 0 ? (
            <Card>
              <CardHeader
                title="What would have to be true"
                description="Derived from the actual feature values and failing checks, not generated prose. Each statement is verifiable against the records."
              />
              <CardBody className="flex flex-col gap-2">
                {exception.counterfactual.statements.map((statement) => (
                  <div
                    key={statement}
                    className="flex gap-2 rounded-lg border border-line bg-surface-2/40 px-3 py-2"
                  >
                    <span aria-hidden className="text-review">
                      ·
                    </span>
                    <span className="text-xs leading-relaxed text-ink-2">
                      {statement}
                    </span>
                  </div>
                ))}
                {exception.counterfactual.blocking.length > 0 ? (
                  <Callout tone="blocked" title="Blocking accounting checks">
                    {exception.counterfactual.blocking.join(", ")}
                  </Callout>
                ) : null}
              </CardBody>
            </Card>
          ) : null}

          {exception.candidates.length > 0 ? (
            <Card>
              <CardHeader
                title={`Candidate links (${exception.candidates.length})`}
                description="Every competing option, not just the one the pipeline preferred."
              />
              <div className="flex flex-col divide-y divide-line/60">
                {exception.candidates.map((candidate) => (
                  <CandidatePanel
                    key={candidate.id}
                    candidate={candidate}
                    recommended={candidate.id === recommendedCandidateId}
                  />
                ))}
              </div>
            </Card>
          ) : (
            <Card>
              <CardHeader title="Candidate links" />
              <CardBody>
                <Callout tone="review" title="No candidate was generated">
                  Nothing in this batch is a plausible counterpart under the blocking
                  rules. That usually means a record is genuinely missing from a source
                  file rather than merely hard to match.
                </Callout>
              </CardBody>
            </Card>
          )}

          {exception.evidence.length > 0 ? (
            <Card>
              <CardHeader
                title="Evidence on file"
                description="Every explanation in this product cites rows that exist here. Contrary evidence is kept, not filtered out."
              />
              <Table>
                <thead>
                  <tr>
                    <Th>Statement</Th>
                    <Th>Direction</Th>
                    <Th>Source</Th>
                  </tr>
                </thead>
                <tbody>
                  {exception.evidence.map((item) => (
                    <tr key={item.id}>
                      <Td className="max-w-xl text-xs leading-relaxed">
                        {item.statement}
                      </Td>
                      <Td className="whitespace-nowrap">
                        {item.supports ? (
                          <Badge tone="proven">supports</Badge>
                        ) : (
                          <Badge tone="blocked">against</Badge>
                        )}
                      </Td>
                      <Td>
                        <Mono>{item.produced_by}</Mono>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>
          ) : null}

          {run ? <AgentTrace run={run} /> : null}
        </div>

        <div className="flex flex-col gap-6">
          <ExceptionActions
            exception={exception}
            recommendedCandidateId={recommendedCandidateId}
          />

          {exception.explanation ? (
            <Card>
              <CardHeader
                title="Explanation"
                description={`Written by the ${
                  exception.explanation_provider ?? "system"
                } provider`}
              />
              <CardBody>
                <p className="text-xs leading-relaxed text-ink-2">
                  {exception.explanation}
                </p>
              </CardBody>
            </Card>
          ) : null}

          <Card>
            <CardHeader title="Why this was not automated" />
            <CardBody className="flex flex-col gap-2.5">
              {exception.best_risk !== null ? (
                <Row
                  label="Estimated error probability"
                  value={exception.best_risk.toFixed(4)}
                />
              ) : null}
              {exception.best_score !== null ? (
                <Row label="Best candidate score" value={exception.best_score.toFixed(4)} />
              ) : null}
              {exception.blocking_invariants.length > 0 ? (
                <div>
                  <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-ink-3">
                    Blocking checks
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {exception.blocking_invariants.map((name) => (
                      <Badge key={name} tone="blocked">
                        {name.replace(/_/g, " ")}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}
              <Row label="Raised" value={dateTime(exception.created_at)} />
            </CardBody>
          </Card>
        </div>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-ink-3">{label}</span>
      <span className="tabular text-xs font-medium text-ink">{value}</span>
    </div>
  );
}

function RecordPanel({ record }: { record: RecordSummary }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="neutral">{titleize(record.record_kind)}</Badge>
        <Mono className="text-ink-2">{record.reference ?? shortId(record.id, 12)}</Mono>
        <span className="tabular text-sm font-semibold text-ink">
          {money(record.amount)}
        </span>
      </div>
      <div className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
        <Row label="Source file" value={titleize(record.source_kind)} />
        <Row
          label="Occurred"
          value={
            record.occurred_at
              ? `${dateTime(record.occurred_at)}${
                  record.timestamp_is_date_only ? " (date only)" : ""
                }`
              : "—"
          }
        />
        {record.counterparty ? (
          <Row label="Counterparty" value={record.counterparty} />
        ) : null}
        <Row label="Status" value={titleize(record.status)} />
      </div>
      {record.timestamp_is_date_only ? (
        <Callout tone="neutral">
          The source gave a date with no time, so any ordering assertion against
          another record on the same day is unsupported by the data. The checks widen
          their tolerance accordingly rather than asserting something the file never
          said.
        </Callout>
      ) : null}
      {record.description ? (
        <div>
          <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-ink-3">
            Narration
          </div>
          <Mono className="block break-words text-ink-2">{record.description}</Mono>
        </div>
      ) : null}
      {Object.keys(record.raw).length > 0 ? (
        <details className="group">
          <summary className="cursor-pointer text-[11px] font-medium text-ink-3 hover:text-ink-2">
            Original source values
          </summary>
          <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-2">
            {Object.entries(record.raw).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-2 border-b border-line/40 py-1">
                <dt className="text-[11px] text-ink-3">{key}</dt>
                <dd className="text-[11px] text-ink-2">
                  <Mono>{value}</Mono>
                </dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </div>
  );
}

/** Features worth showing a reviewer, in the order they matter. */
const SHOWN_FEATURES: Array<[string, string]> = [
  ["reference_exact", "References match exactly"],
  ["reference_containment", "Reference appears in the other narration"],
  ["reference_tail_match", "Trailing reference digits agree"],
  ["reference_similarity", "Reference string similarity"],
  ["amount_exact", "Amounts identical"],
  ["within_window", "Inside expected settlement window"],
  ["day_delta_abs", "Days apart"],
  ["is_sole_candidate", "No competing record"],
  ["competing_candidates", "Competing records"],
];

function CandidatePanel({
  candidate,
  recommended,
}: {
  candidate: CandidateSummary;
  recommended: boolean;
}) {
  const failing = candidate.invariants.filter((check) => !check.passed);
  return (
    <div className="px-5 py-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-ink">
          {RELATION_LABELS[candidate.relation] ?? candidate.relation}
        </span>
        {recommended ? <Badge tone="accent">Agent recommendation</Badge> : null}
        {candidate.score !== null ? (
          <Badge tone="neutral">score {candidate.score.toFixed(4)}</Badge>
        ) : null}
        {candidate.risk !== null ? (
          <Badge tone={candidate.risk <= 0.01 ? "proven" : "review"}>
            risk {candidate.risk.toFixed(4)}
          </Badge>
        ) : null}
        <Mono className="ml-auto">{candidate.generator}</Mono>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <MiniRecord record={candidate.left} label="From" />
        <MiniRecord record={candidate.right} label="To" />
      </div>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {SHOWN_FEATURES.filter(([key]) => key in candidate.features).map(
          ([key, label]) => {
            const value = candidate.features[key];
            const boolean = ["reference_exact", "reference_containment", "reference_tail_match", "amount_exact", "within_window", "is_sole_candidate"].includes(key);
            if (boolean) {
              return (
                <Badge key={key} tone={value > 0 ? "proven" : "neutral"}>
                  {value > 0 ? "✓" : "✕"} {label}
                </Badge>
              );
            }
            return (
              <Badge key={key} tone="neutral">
                {label}: {value % 1 === 0 ? value : value.toFixed(2)}
              </Badge>
            );
          },
        )}
      </div>

      {candidate.evidence.length > 0 ? (
        <ul className="mt-3 flex flex-col gap-1">
          {candidate.evidence.slice(0, 6).map((item) => (
            <li key={item.id} className="flex gap-2 text-[11px] leading-relaxed">
              <span
                aria-hidden
                className={item.supports ? "text-proven" : "text-blocked"}
              >
                {item.supports ? "+" : "−"}
              </span>
              <span className="text-ink-2">{item.statement}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {failing.length > 0 ? (
        <div className="mt-3 rounded-lg border border-blocked/25 bg-blocked-soft/20 px-3 py-2">
          <div className="mb-1 text-[11px] font-semibold text-blocked">
            Failing accounting checks
          </div>
          {failing.map((check) => (
            <p key={check.invariant} className="text-[11px] leading-relaxed text-ink-2">
              {check.invariant.replace(/_/g, " ")}: {check.message}
            </p>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function MiniRecord({ record, label }: { record: RecordSummary; label: string }) {
  return (
    <div className="rounded-lg border border-line bg-surface-2/40 px-3 py-2">
      <div className="text-[11px] font-medium uppercase tracking-wider text-ink-3">
        {label} · {titleize(record.record_kind)}
      </div>
      <div className="tabular mt-1 text-xs font-medium text-ink">
        {money(record.amount)}
      </div>
      <Mono className="mt-0.5 block break-all">
        {record.reference ?? shortId(record.id, 12)}
      </Mono>
      <div className="mt-1 text-[11px] text-ink-3">
        {record.occurred_at ? dateTime(record.occurred_at) : "no timestamp"}
      </div>
    </div>
  );
}
