/**
 * The Hermes chat session a card's worker ran in. The kanban API has no
 * session field on runs; the owner records it when it admits a run's claim, as
 * an `owner_admitted` event (`{session_id, db, …}`, keyed by `run_id`).
 */

import type { KanbanEvent, KanbanTaskDetail } from './types'

export interface KanbanChatSession {
  sessionId: string
  /** The run's profile — the worker's home, which owns the session row. */
  profile: null | string
}

function admittedSessionId(event: KanbanEvent): null | string {
  if (event.kind !== 'owner_admitted') {
    return null
  }

  let payload = event.payload

  if (typeof payload === 'string') {
    try {
      payload = JSON.parse(payload)
    } catch {
      return null
    }
  }

  const id = payload && typeof payload === 'object' ? (payload as Record<string, unknown>).session_id : null

  return typeof id === 'string' && id.trim() ? id.trim() : null
}

/** The latest run's session; when that run was never admitted (still claiming,
 *  or a crash before admission), the newest run that was. Null when no run has
 *  a session yet. */
export function latestRunChatSession(detail: Pick<KanbanTaskDetail, 'events' | 'runs'>): KanbanChatSession | null {
  const admitted = detail.events
    .map(event => ({ event, sessionId: admittedSessionId(event) }))
    .filter((hit): hit is { event: KanbanEvent; sessionId: string } => hit.sessionId !== null)
    .sort((a, b) => b.event.id - a.event.id)

  if (!admitted.length) {
    return null
  }

  const latestRun = detail.runs.at(-1)
  const hit = admitted.find(({ event }) => latestRun && String(event.run_id) === String(latestRun.id)) ?? admitted[0]
  const run = detail.runs.find(candidate => String(candidate.id) === String(hit.event.run_id))

  return { sessionId: hit.sessionId, profile: run?.profile?.trim() || null }
}
