import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("en-IN");
}

/**
 * Format an amount for display.
 *
 * The backend already rendered the digits exactly; this only adds the currency
 * and thousands grouping. It never re-parses the value, because converting the
 * string to a Number and back is precisely the rounding hazard the integer
 * representation exists to prevent.
 */
export function money(amount: { formatted: string; currency: string } | null | undefined): string {
  if (!amount) return "—";
  const symbol = amount.currency === "INR" ? "₹" : `${amount.currency} `;
  const negative = amount.formatted.startsWith("-");
  const digits = negative ? amount.formatted.slice(1) : amount.formatted;
  const [whole, fraction] = digits.split(".");
  return `${negative ? "-" : ""}${symbol}${groupIndian(whole)}${fraction ? `.${fraction}` : ""}`;
}

/** Indian digit grouping: last three, then pairs. */
function groupIndian(whole: string): string {
  if (whole.length <= 3) return whole;
  const head = whole.slice(0, -3);
  const tail = whole.slice(-3);
  const parts: string[] = [];
  let rest = head;
  while (rest.length > 2) {
    parts.unshift(rest.slice(-2));
    rest = rest.slice(0, -2);
  }
  if (rest) parts.unshift(rest);
  return [...parts, tail].join(",");
}

export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return "—";
  return value.length <= length ? value : value.slice(0, length);
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  });
}

export function dateOnly(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function titleize(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

/** Human labels for the canonical relations. */
export const RELATION_LABELS: Record<string, string> = {
  order_to_payment: "Order → Payment",
  payment_to_refund: "Payment → Refund",
  payment_to_settlement: "Payment → Settlement",
  refund_to_settlement: "Refund → Settlement",
  settlement_to_bank_credit: "Settlement → Bank credit",
  fee_to_settlement: "Fee → Settlement",
};

export const METHOD_LABELS: Record<string, string> = {
  exact_reference: "Identifier match",
  exact_composite: "Derived from identifiers",
  calibrated_model: "Calibrated model",
  global_assignment: "Global assignment",
  agent_recommendation: "Agent recommendation",
  human_resolution: "Human decision",
};

export const CATEGORY_LABELS: Record<string, string> = {
  no_candidate: "No candidate found",
  ambiguous_candidates: "Ambiguous candidates",
  missing_reference: "Missing reference",
  amount_mismatch: "Amount mismatch",
  currency_mismatch: "Currency mismatch",
  duplicate_record: "Duplicate record",
  over_refund: "Over-refund",
  unbalanced_allocation: "Unbalanced settlement",
  late_settlement: "Late settlement",
  missing_bank_credit: "Missing bank credit",
  fee_discrepancy: "Fee discrepancy",
  tax_discrepancy: "Tax discrepancy",
  unit_confusion: "Paise/rupee confusion",
  low_confidence: "Low confidence",
  policy_blocked: "Blocked by policy",
};

export const RECORD_KIND_LABELS: Record<string, string> = {
  order: "Order",
  payment: "Payment",
  refund: "Refund",
  settlement: "Settlement",
  bank_credit: "Bank credit",
  fee: "Fee",
  tax: "Tax",
  adjustment: "Adjustment",
  event: "Webhook event",
};
