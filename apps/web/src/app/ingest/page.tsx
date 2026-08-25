import { IngestForm } from "@/components/ingest-form";

export const dynamic = "force-dynamic";

export default function IngestPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          Ingest a batch
        </h1>
        <p className="max-w-3xl text-sm leading-relaxed text-ink-2">
          Upload merchant exports as they arrive: orders, Razorpay payments and
          refunds, the settlement and fee reports, the bank statement, and
          webhook events. A structural fault rejects the whole file. An isolated
          bad row is itemised and the rest proceeds. Reconciliation does not
          start until you run it, so a partial upload cannot post matches.
        </p>
      </div>
      <IngestForm />
    </div>
  );
}
