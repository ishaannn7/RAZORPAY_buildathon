"use client";

/** Triggers the browser's own print dialog — "Save as PDF" is what every
 * browser's print target already offers, so this is the whole PDF story:
 * no PDF-generation library, no server-side rendering step, one button. */
export function PrintButton() {
  return (
    <button
      type="button"
      onClick={() => window.print()}
      className="print:hidden rounded-md border border-accent/40 bg-accent-soft/40 px-3 py-1.5 text-xs font-semibold text-accent transition-colors hover:bg-accent-soft"
    >
      Print / Save as PDF
    </button>
  );
}
