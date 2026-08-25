import { notFound } from "next/navigation";

import { BatchNav } from "@/components/batch-nav";
import {
  Badge,
  Bar,
  Callout,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  Mono,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api } from "@/lib/api";
import { count, dateTime, titleize } from "@/lib/format";

export const dynamic = "force-dynamic";

const SEVERITY_TONE = { high: "blocked", medium: "review", low: "neutral" } as const;

export default async function BatchHealthPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const [batch, anomalies, drift] = await Promise.all([
    api.batch(id),
    api.anomalies(id),
    api.drift(id),
  ]);
  if (!batch) notFound();

  const report = drift?.[0] ?? null;
  const grouped = new Map<string, typeof anomalies>();
  for (const anomaly of anomalies ?? []) {
    const bucket = grouped.get(anomaly.kind) ?? [];
    bucket.push(anomaly);
    grouped.set(anomaly.kind, bucket as typeof anomalies);
  }

  return (
    <div className="flex flex-col gap-6">
      <BatchNav batch={batch} active="health" />

      <Card>
        <CardHeader
          title="Distribution drift"
          description="A threshold's risk guarantee was established on a particular input distribution. When the inputs move, the guarantee is less applicable, so automation tightens rather than loosens."
        />
        <CardBody>
          {!report ? (
            <Callout tone="neutral" title="No comparison available">
              Drift is measured against an earlier batch. This is the first
              reconciled batch, so there is no baseline; inventing one would produce
              a number with no meaning.
            </Callout>
          ) : (
            <div className="flex flex-col gap-4">
              <div className="flex flex-wrap items-center gap-2">
                {report.drift_detected ? (
                  <Badge tone="review">Drift detected</Badge>
                ) : (
                  <Badge tone="proven">Distribution stable</Badge>
                )}
                {report.max_psi !== null ? (
                  <Badge tone="neutral">max PSI {report.max_psi.toFixed(4)}</Badge>
                ) : null}
                {report.triggered_restriction ? (
                  <Badge tone="blocked">Automation tightened</Badge>
                ) : null}
              </div>
              {report.summary ? (
                <p className="text-xs leading-relaxed text-ink-2">{report.summary}</p>
              ) : null}
              <div className="flex flex-col gap-2.5">
                {Object.entries(report.features).map(([feature, detail]) => (
                  <div key={feature}>
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="text-xs text-ink-2">
                        {titleize(feature)}
                      </span>
                      <span className="tabular text-xs text-ink-3">
                        PSI {detail.psi.toFixed(4)} · {detail.verdict}
                      </span>
                    </div>
                    <div className="mt-1">
                      <Bar
                        value={Math.min(detail.psi / 0.5, 1)}
                        tone={
                          detail.verdict === "stable"
                            ? "proven"
                            : detail.verdict === "moderate"
                              ? "review"
                              : "blocked"
                        }
                        label={`${feature} population stability index ${detail.psi.toFixed(4)}`}
                      />
                    </div>
                  </div>
                ))}
              </div>
              <p className="text-[11px] leading-relaxed text-ink-3">
                Measured {dateTime(report.created_at)}. Below 0.1 is stable,
                0.1&nbsp;to&nbsp;0.25 is a moderate shift, above 0.25 is significant.
              </p>
            </div>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Anomalies"
          description="Mostly deterministic rules, because a rule states its reason in terms a finance team can check. The isolation forest covers only the residual case where no rule applies."
        />
        {!anomalies || anomalies.length === 0 ? (
          <EmptyState
            title="No anomalies detected"
            description="No missing bank credits, duplicate references, fee-rate deviations or unusual payment shapes were found."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Severity</Th>
                <Th>Kind</Th>
                <Th>Detector</Th>
                <Th>Finding</Th>
              </tr>
            </thead>
            <tbody>
              {anomalies.map((anomaly) => (
                <tr key={anomaly.id} className="hover:bg-surface-2/40">
                  <Td className="whitespace-nowrap">
                    <Badge
                      tone={
                        SEVERITY_TONE[
                          anomaly.severity as keyof typeof SEVERITY_TONE
                        ] ?? "neutral"
                      }
                    >
                      {anomaly.severity}
                    </Badge>
                  </Td>
                  <Td className="whitespace-nowrap text-xs text-ink-2">
                    {titleize(anomaly.kind)}
                  </Td>
                  <Td>
                    <Mono>{anomaly.detector}</Mono>
                  </Td>
                  <Td className="max-w-2xl text-xs leading-relaxed">
                    {anomaly.summary}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {grouped.size > 0 ? (
        <Card>
          <CardHeader title="Findings by kind" />
          <CardBody className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {[...grouped.entries()].map(([kind, group]) => (
              <div
                key={kind}
                className="rounded-lg border border-line bg-surface-2/40 px-3 py-2"
              >
                <div className="text-[11px] text-ink-3">{titleize(kind)}</div>
                <div className="tabular mt-0.5 text-lg font-semibold text-ink">
                  {count(group?.length ?? 0)}
                </div>
              </div>
            ))}
          </CardBody>
        </Card>
      ) : null}
    </div>
  );
}
