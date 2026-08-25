"use client";

import { useMemo, useState } from "react";

import { Badge, Mono } from "@/components/ui";
import type { GraphEdge, GraphNode, GraphResponse } from "@/lib/api";
import { count, money, RELATION_LABELS, titleize } from "@/lib/format";

/**
 * Layered view of the decided links.
 *
 * Laid out by canonical record kind rather than by a force simulation, because
 * the money flows in one direction: an order becomes a payment, payments and
 * fees roll into a settlement, and a settlement pays out as a bank credit.
 * Columns make that flow legible and keep the layout stable between renders,
 * which a force layout would not.
 */

const COLUMNS: Array<{ kinds: string[]; label: string }> = [
  { kinds: ["order"], label: "Orders" },
  { kinds: ["payment", "refund"], label: "Payments & refunds" },
  { kinds: ["fee", "settlement"], label: "Fees & settlements" },
  { kinds: ["bank_credit"], label: "Bank credits" },
];

const NODE_WIDTH = 132;
const NODE_HEIGHT = 30;
const ROW_GAP = 10;
const COLUMN_GAP = 190;
const PADDING = 28;

const DECISION_STROKE: Record<string, string> = {
  auto_accepted: "var(--color-proven)",
  human_review: "var(--color-review)",
  rejected: "var(--color-blocked)",
};

export function EvidenceGraph({
  graph,
  initialFocusId,
}: {
  graph: GraphResponse;
  /** Pre-select a record, e.g. when arriving from that record's exception page. */
  initialFocusId?: string | null;
}) {
  const [selected, setSelected] = useState<string | null>(initialFocusId ?? null);

  const layout = useMemo(() => buildLayout(graph), [graph]);

  const neighbours = useMemo(() => {
    if (!selected) return null;
    const related = new Set<string>([selected]);
    for (const edge of graph.edges) {
      if (edge.source === selected) related.add(edge.target);
      if (edge.target === selected) related.add(edge.source);
    }
    return related;
  }, [graph.edges, selected]);

  const selectedNode = selected
    ? graph.nodes.find((node) => node.id === selected) ?? null
    : null;
  const selectedEdges = selected
    ? graph.edges.filter((edge) => edge.source === selected || edge.target === selected)
    : [];

  return (
    <div className="flex flex-col gap-4 px-5 py-4">
      <div className="flex flex-wrap items-center gap-3">
        <Legend colour="var(--color-proven)" label="Accepted" />
        <Legend colour="var(--color-review)" label="Needs review" />
        <Legend colour="var(--color-blocked)" label="Rejected" />
        <span className="ml-auto text-[11px] text-ink-3">
          Select a record to isolate its links
        </span>
      </div>

      {/* A visually-hidden orientation note rather than `role="img"` on the SVG
          below: the graph's nodes are real keyboard-operable controls, and
          `role="img"` would collapse them into a flat picture for assistive
          tech, making them unreachable. */}
      <p className="sr-only">
        {`Evidence graph with ${graph.nodes.length} records and ${graph.edges.length} links. Tab through the records below to select one and see its links.`}
      </p>
      <div className="overflow-x-auto rounded-lg border border-line bg-canvas/60">
        <svg
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          className="block"
        >
          {layout.columnLabels.map((column) => (
            <text
              key={column.label}
              x={column.x + NODE_WIDTH / 2}
              y={18}
              textAnchor="middle"
              className="fill-[var(--color-ink-3)] text-[10px] font-medium uppercase"
              style={{ letterSpacing: "0.06em" }}
            >
              {column.label}
            </text>
          ))}

          <g>
            {graph.edges.map((edge, index) => {
              const from = layout.positions.get(edge.source);
              const to = layout.positions.get(edge.target);
              if (!from || !to) return null;
              const dimmed =
                neighbours !== null &&
                !(neighbours.has(edge.source) && neighbours.has(edge.target));
              const startX = from.x + NODE_WIDTH;
              const startY = from.y + NODE_HEIGHT / 2;
              const endX = to.x;
              const endY = to.y + NODE_HEIGHT / 2;
              const midX = (startX + endX) / 2;
              return (
                <path
                  key={`${edge.source}-${edge.target}-${index}`}
                  d={`M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}`}
                  fill="none"
                  stroke={DECISION_STROKE[edge.decision] ?? "var(--color-line-strong)"}
                  strokeWidth={dimmed ? 0.6 : 1.4}
                  strokeOpacity={dimmed ? 0.12 : 0.75}
                  strokeDasharray={edge.method === "human_resolution" ? "4 3" : undefined}
                />
              );
            })}
          </g>

          <g>
            {graph.nodes.map((node) => {
              const position = layout.positions.get(node.id);
              if (!position) return null;
              const dimmed = neighbours !== null && !neighbours.has(node.id);
              const isSelected = selected === node.id;
              const nodeLabel = `${titleize(node.kind)} ${
                node.reference ?? node.id
              }, ${money(node.amount)}${isSelected ? ", selected" : ""}`;
              return (
                <g
                  key={node.id}
                  transform={`translate(${position.x}, ${position.y})`}
                  onClick={() => setSelected(isSelected ? null : node.id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setSelected(isSelected ? null : node.id);
                    }
                  }}
                  role="button"
                  tabIndex={0}
                  aria-pressed={isSelected}
                  aria-label={nodeLabel}
                  className="cursor-pointer focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
                  opacity={dimmed ? 0.25 : 1}
                >
                  <title>{nodeLabel}</title>
                  <rect
                    width={NODE_WIDTH}
                    height={NODE_HEIGHT}
                    rx={6}
                    fill="var(--color-surface-2)"
                    stroke={
                      isSelected ? "var(--color-accent)" : "var(--color-line-strong)"
                    }
                    strokeWidth={isSelected ? 1.8 : 1}
                  />
                  <text
                    x={8}
                    y={12}
                    aria-hidden
                    className="fill-[var(--color-ink-3)] text-[8px] uppercase"
                    style={{ letterSpacing: "0.05em" }}
                  >
                    {titleize(node.kind)}
                  </text>
                  <text
                    x={8}
                    y={23}
                    aria-hidden
                    className="fill-[var(--color-ink)] text-[10px] font-medium"
                    style={{ fontVariantNumeric: "tabular-nums" }}
                  >
                    {money(node.amount)}
                  </text>
                </g>
              );
            })}
          </g>
        </svg>
      </div>

      {selectedNode ? (
        <div className="rounded-lg border border-accent/25 bg-accent-soft/15 px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="accent">{titleize(selectedNode.kind)}</Badge>
            <Mono className="text-ink-2">
              {selectedNode.reference ?? selectedNode.id.slice(0, 12)}
            </Mono>
            <span className="tabular text-xs font-medium text-ink">
              {money(selectedNode.amount)}
            </span>
            <button
              type="button"
              onClick={() => setSelected(null)}
              className="ml-auto text-[11px] text-ink-3 hover:text-ink-2"
            >
              Clear selection
            </button>
          </div>
          <div className="mt-2 flex flex-col gap-1">
            {selectedEdges.map((edge, index) => (
              <EdgeRow key={index} edge={edge} nodes={graph.nodes} focus={selectedNode.id} />
            ))}
          </div>
        </div>
      ) : null}

      {graph.truncated ? (
        <p className="text-[11px] leading-relaxed text-ink-3">
          Showing the first {count(graph.edges.length)} of{" "}
          {count(graph.total_edges)} links. The full set is available through the
          matches view; rendering all of it would stall the browser rather than
          inform anyone.
        </p>
      ) : null}
    </div>
  );
}

function EdgeRow({
  edge,
  nodes,
  focus,
}: {
  edge: GraphEdge;
  nodes: GraphNode[];
  focus: string;
}) {
  const otherId = edge.source === focus ? edge.target : edge.source;
  const other = nodes.find((node) => node.id === otherId);
  const direction = edge.source === focus ? "→" : "←";
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <span aria-hidden className="text-ink-3">
        {direction}
      </span>
      <span className="text-ink-2">
        {RELATION_LABELS[edge.relation] ?? edge.relation}
      </span>
      <Mono>{other?.reference ?? otherId.slice(0, 10)}</Mono>
      <span className="tabular text-ink-3">{money(edge.allocated)}</span>
      <Badge
        tone={
          edge.decision === "auto_accepted"
            ? "proven"
            : edge.decision === "rejected"
              ? "blocked"
              : "review"
        }
      >
        {edge.decision.replace(/_/g, " ")}
      </Badge>
      {edge.blocking.length > 0 ? (
        <Badge tone="blocked" title={edge.blocking.join(", ")}>
          {edge.blocking.length} check failed
        </Badge>
      ) : null}
    </div>
  );
}

function Legend({ colour, label }: { colour: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5 text-[11px] text-ink-3">
      <span
        aria-hidden
        className="h-0.5 w-5 rounded-full"
        style={{ background: colour }}
      />
      {label}
    </span>
  );
}

function buildLayout(graph: GraphResponse) {
  const byColumn = COLUMNS.map(() => [] as GraphNode[]);
  const overflow: GraphNode[] = [];

  for (const node of graph.nodes) {
    const index = COLUMNS.findIndex((column) => column.kinds.includes(node.kind));
    if (index === -1) overflow.push(node);
    else byColumn[index].push(node);
  }
  if (overflow.length) byColumn[byColumn.length - 1].push(...overflow);

  // Ordering by amount keeps the layout deterministic across renders, so the
  // graph does not reshuffle when the page revalidates.
  for (const column of byColumn) {
    column.sort((a, b) => b.amount.subunits - a.amount.subunits);
  }

  const positions = new Map<string, { x: number; y: number }>();
  const columnLabels: Array<{ label: string; x: number }> = [];
  let maxRows = 0;

  byColumn.forEach((column, columnIndex) => {
    const x = PADDING + columnIndex * COLUMN_GAP;
    columnLabels.push({ label: COLUMNS[columnIndex].label, x });
    column.forEach((node, rowIndex) => {
      positions.set(node.id, {
        x,
        y: PADDING + 8 + rowIndex * (NODE_HEIGHT + ROW_GAP),
      });
    });
    maxRows = Math.max(maxRows, column.length);
  });

  return {
    positions,
    columnLabels,
    width: PADDING * 2 + (COLUMNS.length - 1) * COLUMN_GAP + NODE_WIDTH,
    height: PADDING * 2 + 8 + Math.max(maxRows, 1) * (NODE_HEIGHT + ROW_GAP),
  };
}
