import { notFound } from "next/navigation";

import { BatchNav } from "@/components/batch-nav";
import { EvidenceGraph } from "@/components/evidence-graph";
import { Card, CardHeader, EmptyState } from "@/components/ui";
import { api } from "@/lib/api";
import { count } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function GraphPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const [batch, graph] = await Promise.all([
    api.batch(id),
    api.graph(id, "?limit=180"),
  ]);
  if (!batch) notFound();

  return (
    <div className="flex flex-col gap-6">
      <BatchNav batch={batch} active="graph" />

      <Card>
        <CardHeader
          title="Evidence graph"
          description={
            graph
              ? `Showing ${count(graph.edges.length)} of ${count(
                  graph.total_edges,
                )} decided links. Records are nodes; a link is an edge carrying its decision, method and allocated amount.`
              : undefined
          }
        />
        {!graph || graph.edges.length === 0 ? (
          <EmptyState
            title="No links to draw"
            description="Reconcile this batch to populate the graph."
          />
        ) : (
          <EvidenceGraph graph={graph} />
        )}
      </Card>
    </div>
  );
}
