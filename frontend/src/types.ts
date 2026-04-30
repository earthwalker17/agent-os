export interface Message {
  id?: string
  role: 'user' | 'assistant'
  content: string
  timestamp: string
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
}
