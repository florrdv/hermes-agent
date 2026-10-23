import { describe, expect, it } from 'vitest'

import { latestRunChatSession } from './chat-session'
import type { KanbanEvent, KanbanRun } from './types'

const run = (id: number, profile: null | string = 'coder'): KanbanRun => ({ id, profile, status: 'done' })

const admitted = (id: number, runId: number, sessionId: string, asString = false): KanbanEvent => {
  const payload = { db: '/homes/coder/state.db', session_id: sessionId }

  return {
    id,
    kind: 'owner_admitted',
    payload: asString ? JSON.stringify(payload) : payload,
    created_at: id,
    run_id: runId
  }
}

describe('latestRunChatSession', () => {
  it('is null before any run was admitted', () => {
    expect(latestRunChatSession({ events: [], runs: [] })).toBeNull()
    expect(
      latestRunChatSession({
        events: [{ id: 1, kind: 'claimed', payload: {}, created_at: 1, run_id: 7 }],
        runs: [run(7)]
      })
    ).toBeNull()
  })

  it("resolves the latest run's session and its profile", () => {
    expect(
      latestRunChatSession({
        events: [admitted(3, 40, 'local-old'), admitted(9, 41, 'local-new')],
        runs: [run(40, 'old-profile'), run(41, 'coder')]
      })
    ).toEqual({ sessionId: 'local-new', profile: 'coder' })
  })

  it('prefers the latest run over a newer-id event from an earlier run', () => {
    expect(
      latestRunChatSession({
        events: [admitted(5, 41, 'local-latest'), admitted(8, 40, 'local-earlier')],
        runs: [run(40), run(41)]
      })
    ).toMatchObject({ sessionId: 'local-latest' })
  })

  it('falls back to the newest admitted run while the latest one is still claiming', () => {
    expect(
      latestRunChatSession({ events: [admitted(3, 40, 'local-prev', true)], runs: [run(40, 'ops'), run(41)] })
    ).toEqual({ sessionId: 'local-prev', profile: 'ops' })
  })
})
