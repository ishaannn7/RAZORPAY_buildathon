import Link from "next/link";

import {
  Badge,
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
import { api } from "@/lib/api";
import { dateTime, titleize } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const [config, policies] = await Promise.all([api.config(), api.policies()]);

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

  const { agent, review, drift } = config.policy;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <div className="text-xs text-ink-3">
          <Link href="/" className="hover:text-ink-2">
            Batches
          </Link>
          <span aria-hidden> / </span>
          Settings and policies
        </div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          What the agent is allowed to do
        </h1>
        <p className="max-w-3xl text-sm leading-relaxed text-ink-2">
          Authority here is bounded by a tool surface and a set of typed
          limits, not by a prompt asking nicely. Neither the model nor the
          agent can edit this document — the only way it changes is a new
          version being loaded, and every past version stays on record below.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card>
          <CardHeader
            title="Active policy"
            description="Versioned and hashed, so a decision can be re-derived from the policy that produced it."
          />
          <CardBody className="flex flex-col gap-2.5">
            <Row label="Name" value={config.policy.name} />
            <Row label="Version" value={config.policy.version} />
            <Row label="Digest" value={config.policy.digest} mono />
            <Row label="Max risk" value={config.policy.max_risk.toString()} />
            <Row
              label="High-value review"
              value={`₹${(config.policy.high_value_review_subunits / 100).toLocaleString("en-IN")}`}
            />
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Human-review triggers"
            description="Conditions that force a human decision even when the score and risk bound both clear."
          />
          <CardBody className="flex flex-col gap-2.5">
            <Row
              label="On a near-tie"
              value={review.force_on_tie ? "Forces review" : "Not forced"}
            />
            <Row
              label="On an advisory failure"
              value={review.force_on_advisory_failure ? "Forces review" : "Not forced"}
            />
            <Row
              label="Competing candidates above"
              value={review.force_when_competing_candidates_above.toString()}
            />
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Drift response"
            description="The only direction a detected shift is allowed to move automation."
          />
          <CardBody className="flex flex-col gap-2.5">
            <Row label="PSI threshold" value={drift.psi_threshold.toString()} />
            <Row label="On detection" value={titleize(drift.on_detection)} />
            <Row
              label="Risk tightening factor"
              value={`×${drift.risk_tightening_factor}`}
            />
            <Callout tone="neutral">
              A detection multiplies the permitted risk by this factor. It is
              always below 1 — drift can only make automation stricter.
            </Callout>
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Agent authority"
          description="Nine read-only, row-capped tools; nothing that writes a record, executes SQL, or posts a match exists to be called."
        />
        <CardBody className="flex flex-col gap-4">
          <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
            <Metric label="Max iterations" value={agent.max_iterations} />
            <Metric label="Max tool calls" value={agent.max_tool_calls} />
            <Metric label="Max output retries" value={agent.max_output_retries} />
            <Metric
              label="Min cited evidence"
              value={agent.min_cited_evidence}
              hint="Below this, a recommendation is refused regardless of confidence"
            />
          </div>
          <div>
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-ink-3">
              Allowed tools ({agent.allowed_tools.length})
            </div>
            <div className="flex flex-wrap gap-1.5">
              {agent.allowed_tools.map((tool) => (
                <Badge key={tool} tone="neutral">
                  <Mono>{tool}</Mono>
                </Badge>
              ))}
            </div>
          </div>
          <Callout tone={agent.recommendation_requires_human_approval ? "proven" : "blocked"}>
            {agent.recommendation_requires_human_approval
              ? "A recommendation always requires human approval before it can post a match. This is not configurable per case."
              : "Recommendations are not gated on human approval — this would be a serious misconfiguration for a finance workflow."}
          </Callout>
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Policy history"
          description="Every version this instance has ever run under. Nothing here is ever edited or deleted."
        />
        {!policies || policies.length === 0 ? (
          <CardBody>
            <EmptyState
              title="No policy version has been persisted yet"
              description="A version is recorded the first time a batch runs. Until then, the engine still applies the document shown above — it just has nothing to point back to for a replay."
            />
          </CardBody>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Version</Th>
                <Th>Digest</Th>
                <Th>State</Th>
                <Th>Notes</Th>
                <Th align="right">Recorded</Th>
              </tr>
            </thead>
            <tbody>
              {policies.map((version) => (
                <tr key={version.id}>
                  <Td className="whitespace-nowrap text-ink">{version.version}</Td>
                  <Td>
                    <Mono title={version.document_sha256}>
                      {version.document_sha256.slice(0, 16)}
                    </Mono>
                  </Td>
                  <Td>
                    {version.active ? (
                      <Badge tone="proven">Active</Badge>
                    ) : (
                      <Badge tone="neutral">Superseded</Badge>
                    )}
                  </Td>
                  <Td className="max-w-sm text-xs text-ink-3">{version.notes ?? "—"}</Td>
                  <Td align="right" className="tabular whitespace-nowrap">
                    {dateTime(version.created_at)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
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
        <span className="text-xs font-medium text-ink">{value}</span>
      )}
    </div>
  );
}
