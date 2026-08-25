import { Badge, Card, CardBody, CardHeader, Mono } from "@/components/ui";
import type { AgentRunDetail } from "@/lib/api";
import { count, duration, titleize } from "@/lib/format";


/**
 * The investigation trace.
 *
 * Rendered in full rather than summarised, because the point of the agent here
 * is that its reasoning is inspectable. A reviewer needs to see which tools ran,
 * what the verifier concluded, and which hypotheses were discarded — not a
 * confidence score with the work hidden behind it.
 */
export function AgentTrace({ run }: { run: AgentRunDetail }) {
  const recommendation = run.recommendation as
    | {
        candidate_id?: string;
        confidence?: number;
        rationale?: string;
        verified_evidence_ids?: string[];
        invariants_passed?: string[];
        warnings?: string[];
        remaining_uncertainty?: string[];
        requires_human_approval?: boolean;
      }
    | null;

  return (
    <Card>
      <CardHeader
        title="Investigation trace"
        description={`${run.provider}${
          run.model_name ? ` · ${run.model_name}` : " · rule-based"
        } · ${count(run.iterations)} phases · ${count(
          run.tool_calls,
        )} tool calls · ${duration(run.duration_ms)}`}
        action={
          <div className="flex flex-wrap items-center justify-end gap-1.5">
            {run.outcome ? (
              <Badge
                tone={
                  run.outcome === "recommended"
                    ? "accent"
                    : run.outcome === "invalid_output"
                      ? "blocked"
                      : "neutral"
                }
              >
                {titleize(run.outcome)}
              </Badge>
            ) : null}
            {run.denied_tool_calls > 0 ? (
              <Badge tone="blocked" title="The agent reached for a tool it was not permitted to use">
                {run.denied_tool_calls} denied
              </Badge>
            ) : (
              <Badge tone="proven">0 denied</Badge>
            )}
            {run.invalid_outputs > 0 ? (
              <Badge tone="blocked">{run.invalid_outputs} invalid output</Badge>
            ) : null}
          </div>
        }
      />
      <CardBody className="flex flex-col gap-5">
        <ol className="flex flex-col">
          {run.steps.map((step, index) => {
            const isLast = index === run.steps.length - 1;
            const tone =
              step.phase === "verify"
                ? "border-accent bg-accent"
                : step.phase === "abstain"
                  ? "border-review bg-review"
                  : step.phase === "recommend"
                    ? "border-proven bg-proven"
                    : "border-line-strong bg-surface-3";
            return (
              <li key={step.id} className="flex gap-3">
                <div className="flex flex-col items-center">
                  <span
                    aria-hidden
                    className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full border ${tone}`}
                  />
                  {!isLast ? (
                    <span aria-hidden className="my-0.5 w-px flex-1 bg-line" />
                  ) : null}
                </div>
                <div className={`min-w-0 flex-1 ${isLast ? "" : "pb-4"}`}>
                  <div className="flex items-baseline gap-2">
                    <span className="text-xs font-semibold text-ink">
                      {titleize(step.phase)}
                    </span>
                    <span className="tabular text-[11px] text-ink-3">
                      step {step.sequence}
                    </span>
                  </div>
                  {step.thought ? (
                    <p className="mt-1 text-xs leading-relaxed text-ink-2">
                      {step.thought}
                    </p>
                  ) : null}
                  {step.phase === "verify" && step.output ? (
                    <VerifyDetail output={step.output} />
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>

        {run.tools.length > 0 ? (
          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-ink-3">
              Tool calls
            </div>
            <div className="flex flex-col gap-1.5">
              {run.tools.map((call) => (
                <div
                  key={call.id}
                  className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface-2/40 px-3 py-2"
                >
                  <span className="tabular text-[11px] text-ink-3">
                    {call.sequence}
                  </span>
                  <Mono className="text-ink-2">{call.tool_name}</Mono>
                  {call.allowed ? (
                    <Badge tone="proven">allowed</Badge>
                  ) : (
                    <Badge tone="blocked" title={call.denial_reason ?? undefined}>
                      denied
                    </Badge>
                  )}
                  {Object.keys(call.arguments).length > 0 ? (
                    <Mono className="text-[11px] text-ink-3">
                      {Object.keys(call.arguments).join(", ")}
                    </Mono>
                  ) : null}
                  {call.denial_reason ? (
                    <span className="text-[11px] text-blocked">
                      {call.denial_reason}
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-ink-3">
              These nine read-only tools are the agent&apos;s entire reachable surface.
              There is no tool that executes SQL, writes a record or posts a match.
            </p>
          </div>
        ) : null}

        {run.rejected_hypotheses.length > 0 ? (
          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-ink-3">
              Discarded hypotheses
            </div>
            <div className="flex flex-col gap-1.5">
              {run.rejected_hypotheses.map((hypothesis, index) => (
                <div
                  key={index}
                  className="rounded-lg border border-blocked/25 bg-blocked-soft/20 px-3 py-2 text-xs leading-relaxed text-ink-2"
                >
                  {String(hypothesis.reason ?? "rejected")}
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {recommendation ? (
          <div className="rounded-lg border border-accent/25 bg-accent-soft/20 px-4 py-3">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold text-ink">Recommendation</span>
              {recommendation.requires_human_approval ? (
                <Badge tone="review">Requires human approval</Badge>
              ) : null}
              {typeof recommendation.confidence === "number" ? (
                <Badge tone="accent">
                  confidence {recommendation.confidence.toFixed(4)}
                </Badge>
              ) : null}
            </div>
            {recommendation.rationale ? (
              <p className="text-xs leading-relaxed text-ink-2">
                {recommendation.rationale}
              </p>
            ) : null}
            <div className="tabular mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-3">
              <span>
                {count(recommendation.verified_evidence_ids?.length ?? 0)} evidence
                citations verified against the database
              </span>
              <span>
                {count(recommendation.invariants_passed?.length ?? 0)} invariants
                re-checked
              </span>
            </div>
            {recommendation.remaining_uncertainty &&
            recommendation.remaining_uncertainty.length > 0 ? (
              <ul className="mt-2 flex flex-col gap-1">
                {recommendation.remaining_uncertainty.map((item) => (
                  <li key={item} className="text-[11px] leading-relaxed text-review">
                    Still unresolved: {item}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        {run.abstain_reason ? (
          <div className="rounded-lg border border-review/25 bg-review-soft/20 px-4 py-3">
            <div className="mb-1 text-xs font-semibold text-ink">Abstained</div>
            <p className="text-xs leading-relaxed text-ink-2">{run.abstain_reason}</p>
            <p className="mt-2 text-[11px] leading-relaxed text-ink-3">
              Abstaining is a correct outcome. A recommendation the evidence does not
              support would spend a reviewer&apos;s trust for nothing.
            </p>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

function VerifyDetail({ output }: { output: Record<string, unknown> }) {
  const passed = Boolean(output.passed);
  const failures = (output.failures as string[] | undefined) ?? [];
  const hallucinated = (output.hallucinated_evidence as string[] | undefined) ?? [];
  const verified = (output.verified_evidence as string[] | undefined) ?? [];
  const invariantsFailed = (output.invariants_failed as string[] | undefined) ?? [];

  return (
    <div className="mt-2 flex flex-col gap-1.5">
      <div className="flex flex-wrap gap-1.5">
        {passed ? (
          <Badge tone="proven">Verification passed</Badge>
        ) : (
          <Badge tone="blocked">Verification failed</Badge>
        )}
        <Badge tone="neutral">{verified.length} citations resolved</Badge>
        {hallucinated.length > 0 ? (
          <Badge tone="blocked" title="Cited evidence that does not exist">
            {hallucinated.length} fabricated citation
          </Badge>
        ) : null}
        {invariantsFailed.length > 0 ? (
          <Badge tone="blocked">{invariantsFailed.length} invariant failed</Badge>
        ) : null}
      </div>
      {failures.map((failure) => (
        <p key={failure} className="text-[11px] leading-relaxed text-blocked">
          {failure}
        </p>
      ))}
    </div>
  );
}
