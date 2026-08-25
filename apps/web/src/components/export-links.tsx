/**
 * A CSV + JSON pair of export links for one report route. Both routes are
 * the same backend endpoint (`?format=csv|json`), so this is purely a
 * frontend affordance — no new API surface, just two ways to reach it.
 */
export function ExportLinks({
  href,
  label = "Export",
}: {
  href: string;
  label?: string;
}) {
  const separator = href.includes("?") ? "&" : "?";
  return (
    <div className="flex items-center gap-1.5">
      <a
        href={href}
        className="rounded-md border border-line-strong bg-surface-2 px-3 py-1.5 text-xs font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink"
      >
        {label} CSV
      </a>
      <a
        href={`${href}${separator}format=json`}
        className="rounded-md border border-line-strong bg-surface-2 px-3 py-1.5 text-xs font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink"
      >
        {label} JSON
      </a>
    </div>
  );
}
