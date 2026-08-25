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
  Table,
  Td,
  Th,
} from "@/components/ui";
import { ModelRegistry } from "@/components/model-registry";
import { api } from "@/lib/api";
import { count, percent, titleize } from "@/lib/format";

export const dynamic = "force-dynamic";

type LabelStats = {
  total: number;
  positive: number;
  negative: number;
  by_relation: Record<string, { positive: number; negative: number }>;
  by_source: Record<string, number>;
  note: string;
};

export default async function ModelsPage() {
  const [config, labels, models] = await Promise.all([
    api.config(),
    api.labels(),
    api.models(),
  ]);
  const labelStats = labels as LabelStats | null;

  if (!config) {
    return (
      <Card>
        <EmptyState
          title="The API is not reachable"
          description="Start the backend and reload."
        />
      </Card>
    );
  }

  const thresholds = Object.entries(config.scorer.thresholds ?? {});
  const coefficients = Object.entries(config.scorer.coefficients ?? {})
    .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
    .slice(0, 12);
  const maxCoefficient = coefficients.length
    ? Math.max(...coefficients.map(([, value]) => Math.abs(value)))
    : 1;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <div className="text-xs text-ink-3">
          <Link href="/" className="hover:text-ink-2">
            Batches
          </Link>
          <span aria-hidden> / </span>
          Model and policy
        </div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          What governs automation
        </h1>
        <p className="max-w-3xl text-sm leading-relaxed text-ink-2">
          A probability is not a licence to post to a ledger. These are the
          thresholds that decide what may be automated, the evidence behind each
          one, and the policy the agent operates under.
        </p>
      </div>

      <ModelRegistry models={models ?? []} />

      {!config.scorer.trained ? (
        <Card>
          <CardBody>
            <Callout tone="review" title="No scorer has been trained">
              {config.scorer.note}
            </Callout>
          </CardBody>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader
              title="Calibrated thresholds"
              description="Chosen per relation as the lowest value whose one-sided Wilson lower bound on precision still clears the target. A relation whose bound cannot reach it keeps automation off rather than running at an unproven level."
            />
            <Table>
              <thead>
                <tr>
                  <Th>Relation</Th>
                  <Th align="right">Threshold</Th>
                  <Th align="right">Proven precision bound</Th>
                  <Th align="right">Calibration rows</Th>
                  <Th>State</Th>
                </tr>
              </thead>
              <tbody>
                {thresholds.map(([relation, threshold]) => (
                  <tr key={relation}>
                    <Td className="whitespace-nowrap text-ink">
                      {relation.replace(/_/g, " ")}
                    </Td>
                    <Td align="right" className="tabular">
                      {threshold.automation_disabled
                        ? "—"
                        : threshold.accept.toFixed(3)}
                    </Td>
                    <Td align="right" className="tabular">
                      <span
                        className={
                          threshold.precision_lower_bound >= config.target_precision
                            ? "text-proven"
                            : "text-review"
                        }
                      >
                        ≥ {(threshold.precision_lower_bound * 100).toFixed(2)}%
                      </span>
                    </Td>
                    <Td align="right" className="tabular">
                      {count(threshold.calibration_size)}
                    </Td>
                    <Td>
                      {threshold.automation_disabled ? (
                        <Badge tone="review">Automation off</Badge>
                      ) : (
                        <Badge tone="proven">Automating</Badge>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <CardBody className="border-t border-line">
              <div className="grid gap-6 sm:grid-cols-3">
                <Metric
                  label="Target precision"
                  value={percent(config.target_precision, 0)}
                  hint="The bound each threshold must prove"
                />
                <Metric
                  label="Risk budget"
                  value={config.risk_budget.toString()}
                  hint="Maximum permitted error probability"
                />
                <Metric
                  label="Training rows"
                  value={count(config.scorer.training_rows ?? 0)}
                  hint="Fitted on an augmented distribution"
                />
              </div>
              <Callout tone="neutral">
                Using the lower bound rather than the raw ratio matters because 5/5
                and 500/500 are both 100% empirically while carrying very different
                evidence. Thin evidence therefore produces a conservative threshold,
                which is the correct direction.
              </Callout>
            </CardBody>
          </Card>

          {coefficients.length > 0 ? (
            <Card>
              <CardHeader
                title="What the model weighs"
                description="Standardized coefficients. Logistic regression was chosen over gradient boosting so that these map directly onto the features shown to reviewers, keeping counterfactual explanations faithful to the real decision."
              />
              <CardBody className="flex flex-col gap-3">
                {coefficients.map(([feature, value]) => (
                  <div key={feature}>
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="text-xs text-ink-2">
                        {feature.replace(/_/g, " ")}
                      </span>
                      <span className="tabular text-xs text-ink-3">
                        {value > 0 ? "+" : ""}
                        {value.toFixed(3)}
                      </span>
                    </div>
                    <div className="mt-1">
                      <Bar
                        value={Math.abs(value) / maxCoefficient}
                        tone={value > 0 ? "proven" : "blocked"}
                        label={`${feature} coefficient ${value.toFixed(3)}`}
                      />
                    </div>
                  </div>
                ))}
                <p className="text-[11px] leading-relaxed text-ink-3">
                  Positive weights argue for a link; negative weights argue against
                  one.
                </p>
              </CardBody>
            </Card>
          ) : null}
        </>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Active policy"
            description="Versioned and hashed, so a decision can be re-derived from the policy that produced it."
          />
          <CardBody className="flex flex-col gap-2.5">
            <Row label="Name" value={config.policy.name} />
            <Row label="Version" value={config.policy.version} />
            <Row label="Digest" value={config.policy.digest} mono />
            <Row
              label="Maximum permitted risk"
              value={config.policy.max_risk.toString()}
            />
            <Row
              label="High-value review threshold"
              value={`₹${(config.policy.high_value_review_subunits / 100).toLocaleString("en-IN")}`}
            />
            <Callout tone="neutral">
              Neither the model nor the agent can modify the policy that governs it.
              An identifier match is held to a much higher value threshold than a
              statistical one, because an identifier is proof rather than evidence.
            </Callout>
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Investigation provider"
            description="The reconciliation engine's correctness does not depend on which model is installed."
          />
          <CardBody className="flex flex-col gap-2.5">
            <Row label="Active" value={config.llm.name} />
            <Row label="Model" value={config.llm.model ?? "none"} />
            <Row label="Database" value={config.database} />
            {config.llm.is_fallback ? (
              <Callout tone="neutral" title="Running without a language model">
                {config.llm.note} Investigations complete all seven phases on
                deterministic rules, which is what demonstrates the architecture
                rather than asserting it.
              </Callout>
            ) : (
              <Callout tone="accent">
                Every proposal still passes the same deterministic verifier, so the
                set of things the system will accept does not change with the model.
              </Callout>
            )}
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Active learning"
          description="Human resolutions become labelled examples, including the negatives implied by choosing one candidate over its competitors."
        />
        <CardBody>
          {!labelStats || labelStats.total === 0 ? (
            <Callout tone="neutral" title="No human labels yet">
              Resolve an exception in the review workspace and its decision, along
              with the alternatives it rejected, is recorded here as training
              evidence.
            </Callout>
          ) : (
            <div className="flex flex-col gap-4">
              <div className="grid gap-6 sm:grid-cols-3">
                <Metric label="Labels" value={count(labelStats.total)} />
                <Metric
                  label="Positive"
                  value={count(labelStats.positive)}
                  tone="proven"
                  hint="Candidates a reviewer chose"
                />
                <Metric
                  label="Negative"
                  value={count(labelStats.negative)}
                  tone="review"
                  hint="Alternatives the choice rejected"
                />
              </div>
              <Callout tone="neutral">{labelStats.note}</Callout>
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

function Row({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-ink-3">{label}</span>
      {mono ? (
        <Mono className="text-ink-2">{value}</Mono>
      ) : (
        <span className="text-xs font-medium text-ink">{titleize(value)}</span>
      )}
    </div>
  );
}
