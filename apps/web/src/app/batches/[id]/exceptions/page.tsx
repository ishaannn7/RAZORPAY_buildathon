import Link from "next/link";
import { notFound } from "next/navigation";

import { BatchNav } from "@/components/batch-nav";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  Mono,
  StatusBadge,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api, API_BASE } from "@/lib/api";
import { CATEGORY_LABELS, money, shortId, titleize } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function ExceptionQueuePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const [batch, exceptions] = await Promise.all([
    api.batch(id),
    api.exceptions(`?batch_id=${id}&limit=200`),
  ]);
  if (!batch) notFound();

  return (
    <div className="flex flex-col gap-6">
      <BatchNav batch={batch} active="exceptions" />

      <Card>
        <CardHeader
          title="Exception queue"
          description="Work the pipeline could not prove. Every item carries the amount it leaves unexplained, and the sum of those amounts equals the batch's unexplained total."
          action={
            <a
              href={`${API_BASE}/api/batches/${id}/export/exceptions`}
              className="rounded-md border border-line-strong bg-surface-2 px-3 py-1.5 text-xs font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink"
            >
              Export CSV
            </a>
          }
        />
        {!exceptions || exceptions.length === 0 ? (
          <EmptyState
            title="Nothing outstanding"
            description="Every record in this batch reconciled against proven evidence."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th align="right">Amount</Th>
                <Th>Cause</Th>
                <Th>Status</Th>
                <Th>Subject</Th>
                <Th>What is unresolved</Th>
                <Th>Agent</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {exceptions.map((item) => (
                <tr key={item.id} className="transition-colors hover:bg-surface-2/50">
                  <Td align="right" className="tabular whitespace-nowrap font-medium text-ink">
                    {money(item.amount)}
                  </Td>
                  <Td className="whitespace-nowrap">
                    <Badge tone="review">
                      {CATEGORY_LABELS[item.category] ?? titleize(item.category)}
                    </Badge>
                  </Td>
                  <Td>
                    <StatusBadge status={item.status} />
                  </Td>
                  <Td className="whitespace-nowrap">
                    <div className="text-xs text-ink-2">
                      {titleize(item.subject.record_kind)}
                    </div>
                    <Mono>{item.subject.reference ?? shortId(item.subject.id, 10)}</Mono>
                  </Td>
                  <Td className="max-w-lg text-xs leading-relaxed">{item.summary}</Td>
                  <Td className="whitespace-nowrap">
                    {item.agent_outcome ? (
                      <Badge
                        tone={
                          item.agent_outcome === "recommended"
                            ? "accent"
                            : item.agent_outcome === "invalid_output"
                              ? "blocked"
                              : "neutral"
                        }
                      >
                        {titleize(item.agent_outcome)}
                      </Badge>
                    ) : (
                      <span className="text-xs text-ink-3">Not investigated</span>
                    )}
                  </Td>
                  <Td align="right">
                    <Link
                      href={`/exceptions/${item.id}`}
                      className="whitespace-nowrap text-xs font-medium text-accent hover:underline"
                    >
                      Review →
                    </Link>
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
