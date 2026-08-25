import Link from "next/link";
import type { ReactNode } from "react";

import { cn } from "@/lib/format";

export function Card({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border border-line bg-surface/70 backdrop-blur-sm",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  description,
  action,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex items-start justify-between gap-4 border-b border-line px-5 py-4",
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="text-sm font-semibold tracking-tight text-ink">{title}</h2>
        {description ? (
          <p className="mt-1 text-xs leading-relaxed text-ink-3">{description}</p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export function CardBody({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return <div className={cn("px-5 py-4", className)}>{children}</div>;
}

type Tone = "neutral" | "proven" | "review" | "blocked" | "accent";

const TONE_STYLES: Record<Tone, string> = {
  neutral: "border-line-strong/60 bg-surface-2 text-ink-2",
  proven: "border-proven/30 bg-proven-soft/40 text-proven",
  review: "border-review/30 bg-review-soft/40 text-review",
  blocked: "border-blocked/30 bg-blocked-soft/40 text-blocked",
  accent: "border-accent/30 bg-accent-soft/40 text-accent",
};

export function Badge({
  tone = "neutral",
  children,
  className,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-medium leading-5",
        TONE_STYLES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/**
 * A decision badge.
 *
 * The word is always present alongside the colour: a reviewer who cannot
 * distinguish the hues must still be able to tell an accepted link from one
 * awaiting review.
 */
export function DecisionBadge({ decision }: { decision: string }) {
  const map: Record<string, { tone: Tone; label: string }> = {
    auto_accepted: { tone: "proven", label: "Accepted" },
    human_review: { tone: "review", label: "Needs review" },
    rejected: { tone: "blocked", label: "Rejected" },
    unresolved: { tone: "neutral", label: "Unresolved" },
  };
  const entry = map[decision] ?? { tone: "neutral" as Tone, label: decision };
  return <Badge tone={entry.tone}>{entry.label}</Badge>;
}

export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, Tone> = {
    completed: "proven",
    running: "accent",
    ready: "neutral",
    draft: "neutral",
    failed: "blocked",
    validation_failed: "blocked",
    open: "review",
    investigating: "accent",
    awaiting_approval: "review",
    resolved: "proven",
    written_off: "neutral",
  };
  return (
    <Badge tone={map[status] ?? "neutral"}>{status.replace(/_/g, " ")}</Badge>
  );
}

export function Metric({
  label,
  value,
  hint,
  tone = "neutral",
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  const valueTone: Record<Tone, string> = {
    neutral: "text-ink",
    proven: "text-proven",
    review: "text-review",
    blocked: "text-blocked",
    accent: "text-accent",
  };
  return (
    <div className={cn("min-w-0", className)}>
      <div className="text-[11px] font-medium uppercase tracking-wider text-ink-3">
        {label}
      </div>
      <div
        className={cn(
          "tabular mt-1 text-2xl font-semibold leading-tight tracking-tight",
          valueTone[tone],
        )}
      >
        {value}
      </div>
      {hint ? (
        <div className="mt-1 text-xs leading-relaxed text-ink-3">{hint}</div>
      ) : null}
    </div>
  );
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  );
}

export function Th({
  children,
  className,
  align = "left",
}: {
  children?: ReactNode;
  className?: string;
  align?: "left" | "right" | "center";
}) {
  return (
    <th
      scope="col"
      className={cn(
        "border-b border-line px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-ink-3",
        align === "right" && "text-right",
        align === "center" && "text-center",
        align === "left" && "text-left",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  className,
  align = "left",
}: {
  children?: ReactNode;
  className?: string;
  align?: "left" | "right" | "center";
}) {
  return (
    <td
      className={cn(
        "border-b border-line/50 px-4 py-2.5 align-top text-ink-2",
        align === "right" && "text-right",
        align === "center" && "text-center",
        className,
      )}
    >
      {children}
    </td>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
      <div className="text-sm font-medium text-ink-2">{title}</div>
      {description ? (
        <p className="max-w-md text-xs leading-relaxed text-ink-3">{description}</p>
      ) : null}
      {action}
    </div>
  );
}

export function Mono({
  children,
  className,
  title,
}: {
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn("font-mono text-[12px] text-ink-3", className)}
    >
      {children}
    </span>
  );
}

export function Bar({
  value,
  tone = "accent",
  label,
}: {
  value: number;
  tone?: Tone;
  label?: string;
}) {
  const fill: Record<Tone, string> = {
    neutral: "bg-line-strong",
    proven: "bg-proven",
    review: "bg-review",
    blocked: "bg-blocked",
    accent: "bg-accent",
  };
  const clamped = Math.max(0, Math.min(1, value));
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-surface-3"
      role="img"
      aria-label={label ?? `${(clamped * 100).toFixed(1)} percent`}
    >
      <div
        className={cn("h-full rounded-full transition-all", fill[tone])}
        style={{ width: `${clamped * 100}%` }}
      />
    </div>
  );
}

export function Callout({
  tone = "neutral",
  title,
  children,
}: {
  tone?: Tone;
  title?: ReactNode;
  children: ReactNode;
}) {
  const styles: Record<Tone, string> = {
    neutral: "border-line bg-surface-2/60",
    proven: "border-proven/25 bg-proven-soft/25",
    review: "border-review/25 bg-review-soft/25",
    blocked: "border-blocked/25 bg-blocked-soft/25",
    accent: "border-accent/25 bg-accent-soft/25",
  };
  return (
    <div className={cn("rounded-lg border px-4 py-3", styles[tone])}>
      {title ? (
        <div className="mb-1 text-xs font-semibold text-ink">{title}</div>
      ) : null}
      <div className="text-xs leading-relaxed text-ink-2">{children}</div>
    </div>
  );
}

export function NavLink({
  href,
  active,
  children,
}: {
  href: string;
  active?: boolean;
  children: ReactNode;
}) {
  return (
    <Link
      href={href}
      className={cn(
        "rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
        active
          ? "bg-surface-3 text-ink"
          : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
      )}
    >
      {children}
    </Link>
  );
}
