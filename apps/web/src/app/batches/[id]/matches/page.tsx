import { notFound } from "next/navigation";

import { BatchNav } from "@/components/batch-nav";
import {
  Badge,
  Card,
  CardHeader,
  DecisionBadge,
  EmptyState,
  Mono,
  NavLink,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api } from "@/lib/api";
import {
  dateOnly,
  METHOD_LABELS,
  money,
  RELATION_LABELS,
  shortId,
  titleize,
} from "@/lib/format";

export const dynamic = "force-dynamic";

const DECISIONS = [
  { key: "", label: "All" },
  { key: "auto_accepted", label: "Accepted" },
  { key: "human_review", label: "Needs review" },
  { key: "rejected", label: "Rejected" },
];

export default async function MatchesPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ decision?: string }>;
}) {
  const { id } = await params;
  const { decision = "" } = await searchParams;
  const query = decision ? `?decision=${decision}&limit=200` : "?limit=200";
  const [batch, matches] = await Promise.all([api.batch(id), api.matches(id, query)]);
  if (!batch) notFound();

  return (
    <div className="flex flex-col gap-6">
      <BatchNav batch={batch} active="matches" />

      <Card>
        <CardHeader
          title="Decided links"
          description="Each row records how the decision was reached and which checks it satisfied."
          action={
            <div className="flex flex-wrap gap-1">
              {DECISIONS.map((option) => (
                <NavLink
                  key={option.key}
                  href={
                    option.key
                      ? `/batches/${id}/matches?decision=${option.key}`
                      : `/batches/${id}/matches`
                  }
                  active={decision === option.key}
                >
                  {option.label}
                </NavLink>
              ))}
            </div>
          }
        />
        {!matches || matches.length === 0 ? (
          <EmptyState
            title="No links in this view"
            description="Try a different decision filter."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Relation</Th>
                <Th>Decision</Th>
                <Th>How</Th>
                <Th align="right">Allocated</Th>
                <Th align="right">Risk</Th>
                <Th>From</Th>
                <Th>To</Th>
                <Th>Checks</Th>
              </tr>
            </thead>
            <tbody>
              {matches.map((match) => (
                <tr key={match.id} className="hover:bg-surface-2/40">
                  <Td className="whitespace-nowrap text-xs text-ink-2">
                    {RELATION_LABELS[match.relation] ?? match.relation}
                  </Td>
                  <Td>
                    <DecisionBadge decision={match.decision} />
                  </Td>
                  <Td className="whitespace-nowrap text-xs">
                    {METHOD_LABELS[match.method] ?? titleize(match.method)}
                  </Td>
                  <Td align="right" className="tabular whitespace-nowrap">
                    {money(match.allocated)}
                  </Td>
                  <Td align="right" className="tabular whitespace-nowrap">
                    {match.risk === null ? (
                      <span className="text-ink-3">—</span>
                    ) : (
                      <span className={match.risk <= 0.01 ? "text-proven" : "text-review"}>
                        {match.risk.toFixed(4)}
                      </span>
                    )}
                  </Td>
                  <Td className="whitespace-nowrap">
                    <Mono>
                      {match.left.reference ?? shortId(match.left.id, 10)}
                    </Mono>
                    <div className="text-[11px] text-ink-3">
                      {dateOnly(match.left.occurred_at)}
                    </div>
                  </Td>
                  <Td className="whitespace-nowrap">
                    <Mono>
                      {match.right.reference ?? shortId(match.right.id, 10)}
                    </Mono>
                    <div className="text-[11px] text-ink-3">
                      {dateOnly(match.right.occurred_at)}
                    </div>
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      <Badge tone="proven" title={match.invariants_passed.join(", ")}>
                        {match.invariants_passed.length} passed
                      </Badge>
                      {match.invariants_failed.length > 0 ? (
                        <Badge tone="blocked" title={match.invariants_failed.join(", ")}>
                          {match.invariants_failed.length} failed
                        </Badge>
                      ) : null}
                    </div>
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
