/** Task 07.1 — a selectable model provider and its availability. */
export interface ProviderInfo {
  id: string
  label: string
  available: boolean
  default_model: string
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
