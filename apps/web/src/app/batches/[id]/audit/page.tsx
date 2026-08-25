import { notFound } from "next/navigation";

import { BatchNav } from "@/components/batch-nav";
import { ExportLinks } from "@/components/export-links";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  Mono,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api, API_BASE } from "@/lib/api";
import { dateTime, titleize } from "@/lib/format";

export const dynamic = "force-dynamic";

const ACTOR_TONE = {
  system: "neutral",
  pipeline: "accent",
  agent: "review",
  human: "proven",
} as const;

export default async function AuditPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const [batch, entries] = await Promise.all([
    api.batch(id),
    api.audit(id, "?limit=300"),
  ]);
  if (!batch) notFound();

  return (
    <div className="flex flex-col gap-6">
      <BatchNav batch={batch} active="audit" />

      <Card>
        <CardHeader
          title="Audit trail"
          description="Append-only and contiguously sequenced. Agents are never recorded as humans, so an approved link is always distinguishable from an automatic one."
          action={
            <ExportLinks
              href={`${API_BASE}/api/batches/${id}/export/audit`}
              label="Export audit package"
            />
          }
        />
        {!entries || entries.length === 0 ? (
          <EmptyState title="No audit entries" />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th align="right">Seq</Th>
                <Th>Actor</Th>
                <Th>Action</Th>
                <Th>Detail</Th>
                <Th>Input hash</Th>
                <Th>When</Th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id} className="hover:bg-surface-2/40">
                  <Td align="right" className="tabular text-xs text-ink-3">
                    {entry.sequence}
                  </Td>
                  <Td className="whitespace-nowrap">
                    <Badge
                      tone={
                        ACTOR_TONE[entry.actor as keyof typeof ACTOR_TONE] ?? "neutral"
                      }
                    >
                      {entry.actor}
                    </Badge>
                    {entry.actor_detail ? (
                      <div className="mt-0.5 text-[11px] text-ink-3">
                        {entry.actor_detail}
                      </div>
                    ) : null}
                  </Td>
                  <Td className="whitespace-nowrap text-xs text-ink-2">
                    {titleize(entry.action)}
                  </Td>
                  <Td className="max-w-2xl text-xs leading-relaxed">
                    {entry.message}
                  </Td>
                  <Td>
                    {entry.input_sha256 ? (
                      <Mono title={entry.input_sha256}>
                        {entry.input_sha256.slice(0, 10)}
                      </Mono>
                    ) : (
                      <span className="text-ink-3">—</span>
                    )}
                  </Td>
                  <Td className="whitespace-nowrap text-[11px] text-ink-3">
                    {dateTime(entry.created_at)}
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
