/**
 * Kanban worker sessions — transcripts a dispatched worker (`source='kanban'`)
 * writes from its own process. Opening one shows its stored transcript and
 * nothing else: the backend refuses to resume it as an ordinary session
 * (`hermes_cli/kanban_resume_guard.py`, #68779 — a resumed worker would be a
 * second, unsupervised writer in the task's workspace), so the desktop never
 * asks. The worker is steered by commenting on its card.
 *
 * The session row is the authority (`sessions.source`); this store caches the
 * verdict per stored id so the composer, message actions and send gates read
 * one answer after the open resolved the row.
 */
import { atom } from 'nanostores'

import type { SessionInfo } from '@/types/hermes'

export type WorkerSessionState = 'finished' | 'running'

export interface WorkerSession {
  state: WorkerSessionState
  /** The card the worker ran for, when its workspace names it; else null. */
  taskId: null | string
}

type WorkerSessionRow = Pick<SessionInfo, 'cwd' | 'ended_at' | 'is_active' | 'source'>

export const $workerSessions = atom<Readonly<Record<string, WorkerSession>>>({})

export function isWorkerSessionRow<Row extends Pick<SessionInfo, 'source'>>(row: null | Row | undefined): row is Row {
  return row?.source === 'kanban'
}

// Worktree (`<repo>/.worktrees/t_<hex>`) and scratch (`<board>/workspaces/t_<hex>`)
// workspaces are named after the task id kanban_db mints. A `dir:` workspace is
// the user's own folder and names no card.
const TASK_ID_DIR_RE = /[/\\](t_[0-9a-f]+)[/\\]?$/

function workerSessionFromRow(row: WorkerSessionRow): WorkerSession {
  return {
    state: row.ended_at == null && row.is_active ? 'running' : 'finished',
    taskId: row.cwd?.match(TASK_ID_DIR_RE)?.[1] ?? null
  }
}

/** Record a worker session's row (identity kept on a no-op). */
export function recordWorkerSession(storedSessionId: string, row: WorkerSessionRow): void {
  const id = storedSessionId.trim()
  const current = $workerSessions.get()
  const next = workerSessionFromRow(row)
  const known = current[id]

  if (!id || (known && known.state === next.state && known.taskId === next.taskId)) {
    return
  }

  $workerSessions.set({ ...current, [id]: next })
}

export function isWorkerSession(storedSessionId: null | string | undefined): boolean {
  return Boolean(storedSessionId && $workerSessions.get()[storedSessionId.trim()])
}
