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

export type RunStatus = 'running' | 'completed' | 'partial' | 'blocked' | 'failed' | 'unknown'

export type VerificationStatus = 'passed' | 'failed' | 'skipped'

export interface VerificationResult {
  enabled: boolean
  command?: string | null
  status: VerificationStatus
  exit_code?: number | null
  output_preview?: string
  duration_ms?: number | null
}

export interface BrowserVerificationResult {
  enabled: boolean
  command?: string | null
  url?: string | null
  status: VerificationStatus
  screenshot_path?: string | null
  output_preview?: string
  duration_ms?: number | null
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
  verification?: VerificationResult | null
  browser_verification?: BrowserVerificationResult | null
}
