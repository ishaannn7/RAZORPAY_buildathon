import Link from "next/link";

import { NavLink } from "@/components/ui";
import type { BatchDetail } from "@/lib/api";
import { count, money } from "@/lib/format";

export function BatchNav({
  batch,
  active,
}: {
  batch: BatchDetail;
  active: "overview" | "exceptions" | "matches" | "graph" | "audit" | "health";
}) {
  const base = `/batches/${batch.id}`;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <Link href="/" className="text-xs text-ink-3 hover:text-ink-2">
          Batches
        </Link>
        <span aria-hidden className="text-ink-3">
          /
        </span>
        <h1 className="text-lg font-semibold tracking-tight text-ink">
          {batch.name}
        </h1>
        <span className="tabular text-xs text-ink-3">
          {count(batch.record_count)} records · {money(batch.unresolved)} unresolved
        </span>
      </div>
      <nav className="flex flex-wrap gap-1 border-b border-line pb-2">
        <NavLink href={base} active={active === "overview"}>
          Overview
        </NavLink>
        <NavLink href={`${base}/exceptions`} active={active === "exceptions"}>
          Exceptions
          {batch.open_exception_count > 0 ? (
            <span className="tabular ml-1.5 text-review">
              {batch.open_exception_count}
            </span>
          ) : null}
        </NavLink>
        <NavLink href={`${base}/matches`} active={active === "matches"}>
          Matches
        </NavLink>
        <NavLink href={`${base}/graph`} active={active === "graph"}>
          Evidence graph
        </NavLink>
        <NavLink href={`${base}/health`} active={active === "health"}>
          Anomalies &amp; drift
        </NavLink>
        <NavLink href={`${base}/audit`} active={active === "audit"}>
          Audit trail
        </NavLink>
      </nav>
    </div>
  );
}
