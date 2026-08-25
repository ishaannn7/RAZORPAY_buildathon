import Link from "next/link";

import {
  Badge,
  Bar,
  Callout,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  Metric,
  Mono,
  StatusBadge,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api } from "@/lib/api";
import { count, dateTime, money, percent, shortId } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const [batches, config] = await Promise.all([api.batches(), api.config()]);

  if (batches === null) {
    return (
      <Card>
        <EmptyState
          title="The API is not reachable"
          description={
            <>
              Start the backend, then reload. From the repository root:{" "}
              <Mono className="text-ink-2">make api</Mono>
            </>
          }
        />
      </Card>
    );
  }

  if (batches.length === 0) {
    return (
      <Card>
        <EmptyState
          title="No batches yet"
          description={
            <>
              Build the demo state in one command:{" "}
              <Mono className="text-ink-2">make demo</Mono>. It generates the source
              files, fits and calibrates the scorer, reconciles a held-out batch and
              runs the monitoring pass. Or <Link href="/ingest" className="text-accent hover:underline">upload CSVs</Link>.
            </>
          }
        />
      </Card>
    );
  }

  // The reconciled batch is the interesting one; the fitting and calibration
  // datasets are ingested too but are not reconciliation runs.
  const reconciled = batches.filter((batch) => batch.status === "completed");
  const primary = reconciled[0] ?? batches[0];
  const detail = primary ? await api.batch(primary.id) : null;
  const metrics = detail?.metrics ?? null;

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-2">
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          Reconciliation overview
        </h1>
        <p className="max-w-3xl text-sm leading-relaxed text-ink-2">
          Orders, payments, refunds, fees, settlements and bank credits rarely agree.
          ReconProof resolves what the evidence proves, withholds what it cannot, and
          accounts for every rupee in between.
        </p>
      </section>

      {metrics ? (
        <Card>
          <CardHeader
            title={`Latest run — ${primary.name}`}
            description={`${count(metrics.total_records)} records across ${
              detail?.sources.length ?? 0
            } sources, reconciled in ${(metrics.duration_ms / 1000).toFixed(1)}s`}
            action={
              <Link
                href={`/batches/${primary.id}`}
                className="rounded-md border border-line-strong bg-surface-2 px-3 py-1.5 text-xs font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink"
              >
                Open batch
              </Link>
            }
          />
          <CardBody className="grid grid-cols-2 gap-6 lg:grid-cols-4">
            <div>
              <Metric
                label="Automatic match rate"
                value={percent(metrics.automatic_match_rate)}
                tone="proven"
                hint={`${count(metrics.auto_accepted)} accepted, ${count(
                  metrics.sent_to_review,
                )} to review`}
              />
              <div className="mt-3">
                <Bar value={metrics.automatic_match_rate} tone="proven" />
              </div>
            </div>
            <div>
              <Metric
                label="Settlement value traced"
                value={percent(metrics.money_weighted_rate)}
                tone="accent"
                hint={`${money(metrics.settlement_value_traced)} of ${money(
                  metrics.settlement_value,
                )} reached a bank credit`}
              />
              <div className="mt-3">
                <Bar value={metrics.money_weighted_rate} tone="accent" />
              </div>
            </div>
            <Metric
              label="Unexplained value"
              value={money(metrics.unexplained)}
              tone="review"
              hint={
                metrics.unresolved_value_fully_represented
                  ? "Every rupee of it is itemised in the exception queue"
                  : "Warning: the queue does not account for all of it"
              }
            />
            <Metric
              label="Settlements balanced"
              value={`${count(metrics.balanced_settlements)} / ${count(
                metrics.balanced_settlements + metrics.unbalanced_settlements,
              )}`}
              tone={metrics.unbalanced_settlements === 0 ? "proven" : "review"}
              hint="Allocations must sum exactly to the reported net"
            />
          </CardBody>
        </Card>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader
            title="Batches"
            description="Fitting and calibration datasets are ingested alongside the reconciled batch so the evaluation is reproducible."
          />
          <Table>
            <thead>
              <tr>
                <Th>Batch</Th>
                <Th>Status</Th>
                <Th align="right">Records</Th>
                <Th align="right">Open exceptions</Th>
                <Th align="right">Unresolved</Th>
                <Th>Created</Th>
              </tr>
            </thead>
            <tbody>
              {batches.map((batch) => (
                <tr key={batch.id} className="transition-colors hover:bg-surface-2/50">
                  <Td>
                    <Link
                      href={`/batches/${batch.id}`}
                      className="font-medium text-ink hover:text-accent"
                    >
                      {batch.name}
                    </Link>
                    <div className="mt-0.5">
                      <Mono>
                        {shortId(batch.id, 12)}
                        {batch.dataset_seed !== null
                          ? ` · seed ${batch.dataset_seed}`
                          : ""}
                      </Mono>
                    </div>
                  </Td>
                  <Td>
                    <StatusBadge status={batch.status} />
                  </Td>
                  <Td align="right" className="tabular">
                    {count(batch.record_count)}
                  </Td>
                  <Td align="right" className="tabular">
                    {batch.open_exception_count > 0 ? (
                      <Link
                        href={`/batches/${batch.id}/exceptions`}
                        className="text-review hover:underline"
                      >
                        {count(batch.open_exception_count)}
                      </Link>
                    ) : (
                      <span className="text-ink-3">0</span>
                    )}
                  </Td>
                  <Td align="right" className="tabular">
                    {money(batch.unresolved)}
                  </Td>
                  <Td className="whitespace-nowrap text-xs">
                    {dateTime(batch.created_at)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>

        <div className="flex flex-col gap-6">
          <Card>
            <CardHeader
              title="How automation is gated"
              description="A probability is not a licence to post to a ledger."
            />
            <CardBody className="flex flex-col gap-3">
              {config?.scorer.trained ? (
                <>
                  <div className="text-xs leading-relaxed text-ink-2">
                    Thresholds are chosen per relation on held-out calibration data,
                    using a Wilson lower bound on precision. A relation whose bound
                    cannot reach the target keeps automation off rather than running
                    at an unproven threshold.
                  </div>
                  <div className="flex flex-col gap-2">
                    {Object.entries(config.scorer.thresholds ?? {}).map(
                      ([relation, threshold]) => (
                        <div
                          key={relation}
                          className="rounded-lg border border-line bg-surface-2/50 px-3 py-2"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-xs font-medium text-ink-2">
                              {relation.replace(/_/g, " ")}
                            </span>
                            {threshold.automation_disabled ? (
                              <Badge tone="review">Automation off</Badge>
                            ) : (
                              <Badge tone="proven">
                                ≥ {threshold.accept.toFixed(3)}
                              </Badge>
                            )}
                          </div>
                          <div className="tabular mt-1 text-[11px] text-ink-3">
                            precision ≥{" "}
                            {(threshold.precision_lower_bound * 100).toFixed(2)}% on{" "}
                            {count(threshold.calibration_size)} calibration rows
                          </div>
                        </div>
                      ),
                    )}
                  </div>
                  <div className="tabular text-[11px] text-ink-3">
                    Target precision {percent(config.target_precision, 0)} · risk
                    budget {config.risk_budget} · policy {config.policy.name}@
                    {config.policy.version}
                  </div>
                </>
              ) : (
                <Callout tone="review" title="No scorer trained">
                  {config?.scorer.note ??
                    "Deterministic matching still runs. Probabilistic candidates route to review."}
                </Callout>
              )}
            </CardBody>
          </Card>

          {config?.llm.is_fallback ? (
            <Card>
              <CardHeader title="Investigation provider" />
              <CardBody>
                <Callout tone="neutral" title="Running without a language model">
                  {config.llm.note}
                  <div className="mt-2">
                    Investigations complete all seven phases on deterministic rules,
                    which is what demonstrates that reconciliation correctness never
                    depended on the model.
                  </div>
                </Callout>
              </CardBody>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}
