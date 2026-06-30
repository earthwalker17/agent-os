/** Provider Registry 2.0 — capability metadata for one selectable model. */
export interface ModelInfo {
  id: string
  label: string
  /** Accepts image input (vision) — gates chat image upload + visual judgment. */
  vision: boolean
}

/**
 * Task 07.1 + Provider Registry 2.0 — a selectable model provider, its
 * availability, default model, and the capability-tagged model options the
 * per-provider model picker offers.
 */
export interface ProviderInfo {
  id: string
  label: string
  available: boolean
  default_model: string
  /** The provider's selectable models (capability-aware). May be empty. */
  models: ModelInfo[]
}

/** Task 07.0 — metadata for a file attached to a chat message. */
export interface ChatAttachment {
  original_filename: string
  stored_filename: string
  mime_type: string
  size: number
  scope: 'chat' | 'workspace'
  added_to_workspace: boolean
  /** Chat-relative reference (chat_uploads/{conv}/{file}); for bookkeeping. */
  chat_path?: string | null
  /** repo/uploads/{file} when also copied into the project workspace. */
  workspace_path?: string | null
}

export interface Message {
  id?: string
  role: 'user' | 'assistant'
  content: string
  timestamp: string
  /** Arbitrary backend-provided metadata (Task 05.9.5: pending_execution_id). */
  metadata?: Record<string, unknown> | null
  /** Hydrated pending-execution state attached client-side after fetch. */
  pending_execution?: PendingExecution | null
}

export type PendingExecutionStatus = 'pending' | 'dispatched' | 'cancelled'

export interface PendingExecution {
  pending_execution_id: string
  project_id: string
  conversation_id: string
  title: string
  display_plan: string
  task_card: string
  status: PendingExecutionStatus
  run_id?: string | null
  revision_count: number
  created_at: string
  updated_at: string
}

export interface Conversation {
  id: string
  project_id: string
  title: string
  created_at: string
  updated_at: string
}

export interface ProjectContext {
  [filename: string]: string
}

export interface MemoryUpdate {
  filename: string
  section: string
  content: string
  action: 'append' | 'replace'
}

export type RunStatus =
  | 'running'
  | 'completed'
  | 'partial'
  | 'blocked'
  | 'failed'
  | 'cancelled'
  | 'unknown'

/** Run control — one parsed line from a run's events.jsonl timeline. */
export interface RunEvent {
  type: string
  timestamp?: string
  // Events carry varied fields by type (task_id, status, tool_name, command…);
  // the timeline reads them defensively.
  [key: string]: unknown
}

export type VerificationStatus = 'passed' | 'failed' | 'skipped'

/** Task 06.2E — per-command verification outcome. */
export interface VerificationCommandResult {
  command: string
  kind: string
  status: VerificationStatus
  exit_code?: number | null
  output_preview?: string
  duration_ms?: number | null
}

export interface VerificationResult {
  enabled: boolean
  command?: string | null
  status: VerificationStatus
  exit_code?: number | null
  output_preview?: string
  duration_ms?: number | null
  /** Task 06.2E — how commands were chosen + the per-command breakdown. */
  mode?: 'manual' | 'inferred' | 'skipped'
  commands?: VerificationCommandResult[]
  repair_attempts?: number
}

/** One captured page/view from a browser-verification run (multi-page upgrade). */
export interface BrowserPageCapture {
  path: string
  url?: string
  label?: string
  title?: string
  /** 'confirmed' | 'unconfirmed' | 'unknown' — how sure we are the page rendered. */
  readiness?: string
  /** 'primary' | 'link' | 'tab' | 'button' — how the page was reached. */
  nav_kind?: string
}

export interface BrowserVerificationResult {
  enabled: boolean
  command?: string | null
  url?: string | null
  status: VerificationStatus
  screenshot_path?: string | null
  output_preview?: string
  duration_ms?: number | null
  /** Task 06.2C — dependency install step (UI-triggered flow only). */
  install_command?: string | null
  install_status?: VerificationStatus | null
  install_output_preview?: string
  /** Multi-page upgrade — every captured view (primary first). */
  pages?: BrowserPageCapture[]
  /** Primary capture's render readiness: 'confirmed' | 'unconfirmed' | 'unknown'. */
  readiness?: string | null
}

/** AI visual-judgment verdict over the captured screenshots (diagnostic-only). */
export type VisualReviewVerdict = 'passed' | 'warning' | 'failed' | 'inconclusive' | 'skipped'

export interface VisualReviewResult {
  enabled: boolean
  status: VisualReviewVerdict
  headline?: string
  reasoning?: string
  evidence?: string[]
  pages?: { label?: string; verdict?: string; note?: string }[]
  provider?: string | null
  model?: string | null
  duration_ms?: number | null
  skipped_reason?: string
}

/** Phase 5 — per-task status inside a run's execution plan. */
export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped'

/** Phase 5 — one task unit in a run's execution plan/graph. */
export interface ExecutionTask {
  id: string
  title: string
  description?: string
  status: TaskStatus
  depends_on?: string[]
  summary?: string
  files_changed?: string[]
  commands_run?: string[]
  blockers?: string[]
  steps_used?: number
}

/** Phase 6 — Main-Agent recovery assessment of a non-green run. */
export type RecoveryVerdict = 'ok' | 'needs_recovery' | 'exhausted'
export type RecoveryAction = 'inspect' | 'repair' | 'split' | 'reverify' | 'report'

export interface RecoveryAssessment {
  assessed: boolean
  verdict: RecoveryVerdict
  diagnosis?: string
  recommended_action: RecoveryAction
  /** A self-contained follow-up task card — present only for run-type actions. */
  follow_up_task_card?: string
  rationale?: string
  error?: string | null
}

/** Phase 5 — the run's persisted plan + task graph. */
export interface ExecutionPlan {
  goal?: string
  analysis?: string
  risks?: string[]
  tasks?: ExecutionTask[]
  /** 'planned' (LLM-decomposed), 'simple' (single task), 'fallback' (planning failed). */
  mode?: 'planned' | 'simple' | 'fallback'
  created_at?: string
}

export interface RunRecord {
  run_id: string
  project_id: string
  task_title: string
  status: RunStatus
  created_at?: string
  completed_at?: string | null
  files_changed?: string[]
  commands_run?: string[]
  blockers?: string[]
  /** Task 06.2D — concise run summary, mirrors result.md's Summary section. */
  summary?: string
  verification?: VerificationResult | null
  browser_verification?: BrowserVerificationResult | null
  /** AI visual judgment over browser-verification screenshots (diagnostic-only). */
  visual_review?: VisualReviewResult | null
  /**
   * Task 06.2E — transient sub-status for the automatic command-verification
   * phase: 'verifying' while inferred/manual commands run, 'repairing' during
   * the one bounded repair pass, null once settled. The UI treats both as
   * in-progress.
   */
  verification_state?: 'verifying' | 'repairing' | null
  /**
   * Task 06.2D — transient sub-status for the user-triggered browser
   * verification flow: 'running' while install + dev server + screenshot is in
   * flight, then the terminal verification status. Only 'running' is treated as
   * in-progress by the UI.
   */
  browser_verification_state?: 'running' | 'passed' | 'failed' | null
  /** Phase 5 — the run's execution plan + task graph (null for older runs). */
  plan?: ExecutionPlan | null
  /** Run control — set true while a cancel is pending (status still 'running'). */
  cancel_requested?: boolean
  /** Run control — retry linkage: the run this one was retried from / into. */
  retry_of?: string | null
  retried_by?: string | null
  /**
   * Task 06.0 — post-run memory reconciliation outcome (shipped on the wire
   * since 06.0; typed here in Phase 6). `memory_reconciled` is true/false once
   * the reconciler ran (null before), `memory_reconciliation` is a short tag
   * ('applied' / 'skipped_*' / 'error').
   */
  memory_reconciled?: boolean | null
  memory_reconciliation?: string | null
  memory_reconciliation_error?: string | null
  /** Phase 6.1 — the reconciliation judge's one-sentence reason (applied or skipped). */
  memory_reconciliation_reason?: string | null
  /** Phase 6 — Main-Agent recovery assessment for a non-green run (null if green). */
  recovery_assessment?: RecoveryAssessment | null
  /** Phase 6.1 — recovery lineage + budget. ``recovery_of`` is the run this one
   * was auto-recovered from; ``recovered_by`` is the recovery run spawned from
   * this one (set once a recovery exists, whether auto or confirmed). */
  recovery_of?: string | null
  recovered_by?: string | null
  /** Remaining user-approved auto-recovery attempts (0 = none). */
  recovery_budget?: number
  /** Recovery chain depth (0 = original). */
  orchestration_round?: number
  /**
   * Phase 7 — Project Ops (Git/GitHub) linkage. Scalar refs only; the full diff
   * lives in the per-run diff.patch artifact (fetched on demand via /diff), never
   * inline. All optional so older runs render unchanged. Never carries a secret.
   */
  pre_run_checkpoint?: string | null
  checkpoint_tag?: string | null
  base_commit?: string | null
  head_commit?: string | null
  branch?: string | null
  commit_sha?: string | null
  pushed?: boolean
  pr_url?: string | null
  pr_number?: number | null
  diff_stat?: string | null
  /** Transient sub-status while a Git action runs; null at rest. UI treats as in-progress. */
  git_state?: 'checkpointing' | 'committing' | 'pushing' | 'opening_pr' | 'rolling_back' | null
  /**
   * Phase 8 — Production Path (Vercel deploy) linkage. Scalar refs only; never a
   * secret. All optional so older runs render unchanged. Project-level
   * provisioning facts (Stripe/Supabase) live in the OPS.md ledger, not here.
   */
  deployment_id?: string | null
  deployment_url?: string | null
  deployment_target?: 'preview' | 'production' | null
  /** Transient sub-status while a deploy/redeploy/rollback runs; null at rest. */
  deploy_state?: 'deploying' | 'building' | 'redeploying' | 'rolling_back' | null
  /** Umbrella transient sub-status for any in-flight external action (UI poll gate). */
  external_state?: string | null
}

/** Task 06.2D — managed preview dev-server status. */
export interface PreviewStatus {
  project_id: string
  running: boolean
  url?: string | null
  command?: string | null
  started_at?: string | null
  has_package_json?: boolean
  /**
   * Task 06.2E — node_modules is present on disk, so the project is
   * preview-ready regardless of whether browser verification was run.
   */
  deps_installed?: boolean
}

/** Phase 7 — live Git working-tree status for a project. */
export interface GitStatus {
  is_repo: boolean
  branch?: string | null
  dirty?: boolean
  untracked?: number
  modified?: number
  staged?: number
  head?: string | null
  error?: string | null
}

/** Phase 7 — GitHub connector presence + connectivity (never the token value). */
export interface GitHubConnectorStatus {
  provider: 'github'
  configured: boolean
  connected: boolean
  scope: 'project' | 'global' | 'none'
  source: 'project' | 'global_file' | 'env' | 'none'
  login?: string | null
  default_remote?: string | null
  error?: string | null
}

/** Phase 7 — credential presence/metadata for a provider (never the value). */
export interface CredentialStatus {
  provider: string
  configured: boolean
  source: 'project' | 'global_file' | 'env' | 'none'
  scope: 'project' | 'global' | 'none'
  login?: string | null
  default_remote?: string | null
}

/**
 * Phase 7 — an External Action Contract returned by a Git/GitHub endpoint in
 * preview (confirm:false) mode: what would happen, shown to the user before they
 * confirm. The same endpoint executes when called again with confirm:true.
 */
export interface GitActionContract {
  action: 'commit' | 'push' | 'pr' | 'rollback'
  title: string
  external?: boolean
  destructive?: boolean
  requires_confirmation?: boolean
  branch?: string | null
  remote?: string | null
  target?: string | null
  files?: string[]
  refused?: string[]
  diff_stat?: string | null
  message?: string
  pr_title?: string
  base?: string
  head?: string | null
  checkpoint?: string
  summary?: string
  token_configured?: boolean
  pushed?: boolean
}

/** Phase 7 — wrapper response from a two-phase Git/GitHub action endpoint. */
export interface GitActionResponse {
  contract: GitActionContract
  applied: boolean
  run: RunRecord
  commit_sha?: string | null
  refused?: string[]
}

/* ------------------------------------------------------------------ */
/* Phase 8 — Production Path connectors, env registry, external actions */
/* ------------------------------------------------------------------ */

export type ConnectorProvider = 'github' | 'vercel' | 'supabase' | 'stripe'

/** Phase 8 — presence-only status for any provider (never a value). The
 * provider-specific metadata fields (login / username / project_ref / …) are
 * optional and only set for the relevant provider. */
export interface ConnectorStatus {
  provider: string
  configured: boolean
  source: 'project' | 'global_file' | 'env' | 'none'
  scope: 'project' | 'global' | 'none'
  /** Presence (boolean) for each of the provider's secret fields — never values. */
  secret_fields?: Record<string, boolean>
  // github
  login?: string | null
  default_remote?: string | null
  // vercel
  username?: string | null
  org_id?: string | null
  project_id?: string | null
  // supabase
  project_ref?: string | null
  url?: string | null
  anon_key?: string | null
  // stripe
  account?: string | null
  publishable_key?: string | null
}

/** Phase 8 — every provider's status, keyed by provider id (GET /connectors). */
export type ConnectorStatusMap = Record<ConnectorProvider, ConnectorStatus>

/** Phase 8 — live Vercel connector status (presence + linked project). */
export interface VercelStatus extends ConnectorStatus {
  connected?: boolean
  linked?: boolean
  error?: string | null
}

/** Phase 8 — one app-env var, presence-only (the built app's env; never a value). */
export interface EnvVarEntry {
  key: string
  targets: string[]
  secret: boolean
  is_set: boolean
}

export type EnvRegistry = EnvVarEntry[]

/** Phase 8 — external-action kinds the generic contract panel drives. */
export type ExternalActionKind =
  | 'deploy'
  | 'redeploy'
  | 'rollback'
  | 'env_set'
  | 'migration_apply'
  | 'link_project'

/** Phase 8 — an External Action Contract (preview on confirm:false). Superset of
 * the per-action fields; the panel reads only what each kind populates. */
export interface ExternalActionContract {
  action: string
  title: string
  external?: boolean
  destructive?: boolean
  requires_confirmation?: boolean
  mode?: 'test' | 'live'
  live_gate_passed?: boolean
  target?: string | null
  token_configured?: boolean
  linked?: boolean
  // deploy / redeploy / rollback
  git_ref?: string | null
  commit?: string | null
  source_deployment_id?: string | null
  with_latest_commit?: boolean
  current?: string | null
  summary?: string
  // env_set
  key?: string
  targets?: string[]
  type?: 'sensitive' | 'encrypted' | 'plain'
  value_configured?: boolean
  env_id?: string | null
  // supabase migration / link
  pending?: string
  diff?: string | null
  diff_available?: boolean
  docker_note?: string | null
  include_seed?: boolean
}

/** Phase 8 — wrapper response from a two-phase external-action endpoint. */
export interface ExternalActionResponse {
  contract: ExternalActionContract
  applied: boolean
  async?: boolean
  run?: RunRecord
}
