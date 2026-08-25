import type { Metadata } from "next";
import Link from "next/link";

import { api } from "@/lib/api";
import { Badge } from "@/components/ui";

import "./globals.css";

export const metadata: Metadata = {
  title: "ReconProof — evidence-linked reconciliation",
  description:
    "Reconciles orders, payments, refunds, fees, settlements and bank credits. Automates only what it can prove, and accounts for every rupee it cannot.",
};

async function ProviderPill() {
  const health = await api.health();
  if (!health) {
    return (
      <Badge tone="blocked" title="The API is not reachable">
        API offline
      </Badge>
    );
  }
  return (
    <div className="flex items-center gap-2">
      <Badge
        tone={health.model_stage === "production" ? "proven" : "review"}
        title={
          health.model_stage === "production"
            ? "A calibrated scorer is governing automation"
            : "No scorer promoted; deterministic matching only"
        }
      >
        {health.model_stage === "production" ? "Calibrated model" : "Rules only"}
      </Badge>
      <Badge
        tone={health.llm_provider === "deterministic" ? "neutral" : "accent"}
        title={
          health.llm_provider === "deterministic"
            ? "No language model reachable; investigations run on deterministic rules"
            : `Investigations use ${health.llm_provider}`
        }
      >
        {health.llm_provider === "deterministic"
          ? "No LLM · rules"
          : `LLM · ${health.llm_provider}`}
      </Badge>
    </div>
  );
}

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <div className="flex min-h-screen flex-col">
          <header className="sticky top-0 z-20 border-b border-line bg-canvas/85 backdrop-blur">
            <div className="mx-auto flex w-full max-w-[1400px] items-center gap-4 px-4 py-3 sm:px-6">
              <Link href="/" className="flex items-center gap-2.5">
                <span
                  aria-hidden
                  className="flex h-7 w-7 items-center justify-center rounded-md border border-accent/40 bg-accent-soft/50 text-[13px] font-bold text-accent"
                >
                  R
                </span>
                <span className="text-sm font-semibold tracking-tight text-ink">
                  ReconProof
                </span>
              </Link>
              <span className="hidden text-xs text-ink-3 sm:inline">
                Evidence-linked financial reconciliation
              </span>
              <nav aria-label="Primary" className="ml-auto flex items-center gap-3">
                <Link
                  href="/ingest"
                  className="text-xs font-medium text-ink-3 transition-colors hover:text-ink"
                >
                  Ingest
                </Link>
                <Link
                  href="/architecture"
                  className="hidden text-xs font-medium text-ink-3 transition-colors hover:text-ink sm:inline"
                >
                  Architecture
                </Link>
                <Link
                  href="/models"
                  className="text-xs font-medium text-ink-3 transition-colors hover:text-ink"
                >
                  Model &amp; policy
                </Link>
                <Link
                  href="/settings"
                  className="hidden text-xs font-medium text-ink-3 transition-colors hover:text-ink sm:inline"
                >
                  Settings
                </Link>
                <ProviderPill />
              </nav>
            </div>
          </header>

          <main className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-6 sm:px-6">
            {children}
          </main>

          <footer className="border-t border-line px-4 py-4 sm:px-6">
            <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-1 text-[11px] leading-relaxed text-ink-3 sm:flex-row sm:items-center sm:justify-between">
              <span>
                All figures are measured on seeded synthetic data. Nothing here is a
                real merchant result.
              </span>
              <span>
                Automation is gated on a proven risk bound, not a model probability.
              </span>
            </div>
          </footer>
        </div>
      </body>
    </html>
  );
}
