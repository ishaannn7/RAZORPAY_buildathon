/**
 * Typed client for the ReconProof API.
 *
 * Amounts arrive as integer minor units plus a preformatted string. The UI only
 * ever displays the string and compares the integer; it never parses the string
 * back into a number, which is how float rounding would sneak back in after the
 * backend went to the trouble of avoiding it.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8817";

export type Amount = {
  subunits: number;
  currency: string;
  formatted: string;
};

export type BatchSummary = {
  id: string;
  name: string;
  status: string;
  currency: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  record_count: number;
  exception_count: number;
  open_exception_count: number;
  unresolved: Amount | null;
  automation_restricted: boolean;
  restriction_reason: string | null;
  dataset_seed: number | null;
};

export type BatchMetrics = {
  total_records: number;
  records_by_kind: Record<string, number>;
  exact_links: number;
  candidates_generated: number;
  auto_accepted: number;
  sent_to_review: number;
  rejected_by_invariant: number;
  displaced_by_assignment: number;
  forced_review_by_tie: number;
  balanced_settlements: number;
  unbalanced_settlements: number;
  automatic_match_rate: number;
  money_weighted_rate: number;
  settlement_value: Amount;
  settlement_value_traced: Amount;
  unexplained: Amount;
  exception_total: Amount;
  unresolved_value_fully_represented: boolean;
  exceptions_by_category: Record<string, number>;
  scorer: string;
  accept_threshold: number | null;
  risk_budget: number | null;
  duration_ms: number;
};

export type SourceFileSummary = {
  id: string;
  source_kind: string;
  filename: string;
  content_sha256: string;
  byte_size: number;
  row_count: number;
  accepted_rows: number;
  rejected_rows: number;
  detected_columns: Record<string, string>;
  validation_errors: Array<Record<string, unknown>>;
};

export type BatchDetail = BatchSummary & {
  sources: SourceFileSummary[];
  metrics: BatchMetrics | null;
  policy_version: string | null;
  model_version: string | null;
};

export type RecordSummary = {
  id: string;
  source_kind: string;
  record_kind: string;
  external_id: string | null;
  reference: string | null;
  amount: Amount;
  occurred_at: string | null;
  timestamp_is_date_only: boolean;
  description: string | null;
  counterparty: string | null;
  payment_method: string;
  status: string;
  raw: Record<string, string>;
};

export type EvidenceSummary = {
  id: string;
  kind: string;
  statement: string;
  supports: boolean;
  weight: number;
  produced_by: string;
  detail: Record<string, unknown>;
};

export type InvariantSummary = {
  invariant: string;
  passed: boolean;
  message: string | null;
  detail: Record<string, unknown>;
};

export type CandidateSummary = {
  id: string;
  relation: string;
  generator: string;
  score: number | null;
  risk: number | null;
  features: Record<string, number>;
  left: RecordSummary;
  right: RecordSummary;
  evidence: EvidenceSummary[];
  invariants: InvariantSummary[];
};

export type MatchSummary = {
  id: string;
  relation: string;
  decision: string;
  method: string;
  score: number | null;
  risk: number | null;
  allocated: Amount;
  rationale: string | null;
  invariants_passed: string[];
  invariants_failed: string[];
  left: RecordSummary;
  right: RecordSummary;
};

export type ExceptionSummary = {
  id: string;
  batch_id: string;
  category: string;
  status: string;
  amount: Amount;
  summary: string;
  best_score: number | null;
  best_risk: number | null;
  blocking_invariants: string[];
  explanation: string | null;
  explanation_provider: string | null;
  created_at: string;
  subject: RecordSummary;
  agent_run_id: string | null;
  agent_outcome: string | null;
};

export type CounterfactualSummary = {
  statements: string[];
  blocking: string[];
};

export type ResolutionSummary = {
  id: string;
  reviewer: string;
  action: string;
  notes: string | null;
  chosen_candidate_id: string | null;
  overrode_agent: boolean;
  created_at: string;
};

export type ExceptionDetail = ExceptionSummary & {
  candidates: CandidateSummary[];
  evidence: EvidenceSummary[];
  counterfactual: CounterfactualSummary | null;
  resolution: ResolutionSummary | null;
};

export type AgentStepSummary = {
  id: string;
  sequence: number;
  phase: string;
  thought: string | null;
  output: Record<string, unknown> | null;
  duration_ms: number | null;
};

export type ToolCallSummary = {
  id: string;
  sequence: number;
  tool_name: string;
  arguments: Record<string, unknown>;
  allowed: boolean;
  denial_reason: string | null;
  result_summary: string | null;
  result_row_count: number | null;
  error: string | null;
  duration_ms: number | null;
};

export type AgentRunDetail = {
  id: string;
  batch_id: string;
  exception_id: string | null;
  kind: string;
  phase: string;
  outcome: string | null;
  provider: string;
  model_name: string | null;
  iterations: number;
  tool_calls: number;
  denied_tool_calls: number;
  invalid_outputs: number;
  duration_ms: number | null;
  recommendation: Record<string, unknown> | null;
  cited_evidence_ids: string[];
  rejected_hypotheses: Array<Record<string, unknown>>;
  abstain_reason: string | null;
  created_at: string;
  steps: AgentStepSummary[];
  tools: ToolCallSummary[];
};

export type AuditEntry = {
  id: string;
  sequence: number;
  action: string;
  actor: string;
  actor_detail: string | null;
  subject_type: string | null;
  subject_id: string | null;
  message: string | null;
  detail: Record<string, unknown>;
  input_sha256: string | null;
  created_at: string;
};

export type AnomalySummary = {
  id: string;
  detector: string;
  kind: string;
  severity: string;
  score: number | null;
  summary: string;
  detail: Record<string, unknown>;
};

export type DriftSummary = {
  id: string;
  drift_detected: boolean;
  max_psi: number | null;
  triggered_restriction: boolean;
  summary: string | null;
  features: Record<string, { psi: number; verdict: string; reference_mean: number; current_mean: number }>;
  created_at: string;
};

export type BriefingSummary = {
  id: string;
  headline: string;
  body: string;
  recommended_actions: string[];
  cited_metrics: Record<string, unknown>;
  provider: string;
  created_at: string;
};

export type GraphNode = {
  id: string;
  kind: string;
  source: string;
  amount: Amount;
  reference: string | null;
  occurred_at: string | null;
};

export type GraphEdge = {
  source: string;
  target: string;
  relation: string;
  decision: string;
  method: string;
  score: number | null;
  risk: number | null;
  allocated: Amount;
  blocking: string[];
};

export type GraphResponse = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  total_edges: number;
};

export type HealthResponse = {
  status: string;
  database: string;
  llm_provider: string;
  llm_available: boolean;
  model_stage: string | null;
  semantic_matching: boolean;
  version: string;
};

export type RuntimeConfig = {
  target_precision: number;
  risk_budget: number;
  policy: {
    name: string;
    version: string;
    digest: string;
    max_risk: number;
    high_value_review_subunits: number;
  };
  scorer: {
    trained: boolean;
    note?: string;
    training_rows?: number;
    relations_calibrated?: string[];
    thresholds?: Record<
      string,
      {
        accept: number;
        precision_lower_bound: number;
        calibration_size: number;
        automation_disabled: boolean;
      }
    >;
    coefficients?: Record<string, number>;
  };
  llm: {
    name: string;
    model: string | null;
    is_fallback: boolean;
    note: string | null;
  };
  database: string;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type FetchOptions = {
  method?: string;
  body?: unknown;
  /** Server components read fresh data; the demo is meant to reflect real state. */
  cache?: RequestCache;
  signal?: AbortSignal;
};

export async function apiFetch<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const { method = "GET", body, cache = "no-store", signal } = options;
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    cache,
    signal,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload?.detail) {
        message =
          typeof payload.detail === "string"
            ? payload.detail
            : JSON.stringify(payload.detail);
      }
    } catch {
      // Body was not JSON; the status line is the best available message.
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * Fetch that resolves to null instead of throwing when the API is unreachable.
 *
 * Used by server components so a stopped backend renders an explanatory empty
 * state rather than a Next.js error page.
 */
export async function apiFetchSafe<T>(
  path: string,
  options: FetchOptions = {},
): Promise<T | null> {
  try {
    return await apiFetch<T>(path, options);
  } catch {
    return null;
  }
}

export type ModelVersionSummary = {
  id: string;
  name: string;
  kind: string;
  stage: string;
  accept_threshold: number | null;
  risk_budget: number | null;
  feature_names: string[];
  promoted_at: string | null;
  promoted_by: string | null;
  notes: string | null;
  created_at: string;
  gates_passed: boolean | null;
  gate_failures: string[];
  precision_lower_bound: number | null;
  coverage: number | null;
};

export const api = {
  health: () => apiFetchSafe<HealthResponse>("/api/health"),
  config: () => apiFetchSafe<RuntimeConfig>("/api/config"),
  batches: () => apiFetchSafe<BatchSummary[]>("/api/batches"),
  batch: (id: string) => apiFetchSafe<BatchDetail>(`/api/batches/${id}`),
  matches: (id: string, params = "") =>
    apiFetchSafe<MatchSummary[]>(`/api/batches/${id}/matches${params}`),
  graph: (id: string, params = "") =>
    apiFetchSafe<GraphResponse>(`/api/batches/${id}/graph${params}`),
  audit: (id: string, params = "") =>
    apiFetchSafe<AuditEntry[]>(`/api/batches/${id}/audit${params}`),
  exceptions: (params = "") =>
    apiFetchSafe<ExceptionSummary[]>(`/api/exceptions${params}`),
  exception: (id: string) => apiFetchSafe<ExceptionDetail>(`/api/exceptions/${id}`),
  agentRun: (id: string) => apiFetchSafe<AgentRunDetail>(`/api/agent/runs/${id}`),
  anomalies: (id: string) =>
    apiFetchSafe<AnomalySummary[]>(`/api/monitoring/${id}/anomalies`),
  drift: (id: string) => apiFetchSafe<DriftSummary[]>(`/api/monitoring/${id}/drift`),
  briefings: (id: string) =>
    apiFetchSafe<BriefingSummary[]>(`/api/agent/briefing/${id}`),
  labels: () => apiFetchSafe<Record<string, unknown>>("/api/models/labels"),
  models: () => apiFetchSafe<ModelVersionSummary[]>("/api/models"),
};
