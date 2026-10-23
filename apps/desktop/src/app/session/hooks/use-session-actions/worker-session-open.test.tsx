import { act, cleanup, render } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import { getLatestSessionMessages, type SessionInfo } from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import * as gateways from '@/store/gateway'
import { $activeGatewayProfile, $showAllProfiles } from '@/store/profile'
import { isStoredTranscriptReadOnly } from '@/store/read-only-transcript'
import { $activeSessionId, $messages, _resetSessionOwnerHintsForTests, setConnection, setSessions } from '@/store/session'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'
import { $workerSessions } from '@/store/worker-sessions'

import type { ClientSessionState } from '../../../types'

import { useSessionActions } from './index'

vi.mock('@/app/contrib/hooks/use-background-sync', () => ({ resetLiveRuntimeTracking: vi.fn() }))
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn(async () => ({
    messages: [{ content: 'task brief', id: 1, role: 'user', timestamp: 1 }],
    session_id: 'stored'
  })),
  getProfiles: vi.fn(async () => ({ profiles: [{ name: 'default' }] }))
}))

const rpcResult = (method: string) =>
  method === 'session.resume' ? { info: {}, messages: [], resumed: 'stored', session_id: 'runtime' } : {}

const local = {
  authMode: 'token',
  baseUrl: 'http://local.invalid',
  connectionId: 'local',
  mode: 'local',
  profile: 'default',
  registryScoped: false,
  token: 'test',
  wsUrl: 'ws://local.invalid/ws'
} as HermesConnection

type Resume = ReturnType<typeof useSessionActions>['resumeSession']

function Harness({ onReady }: { onReady: (resume: Resume) => void }) {
  const states = useRef(new Map<string, ClientSessionState>())
  const activeSessionIdRef = useRef<string | null>(null)
  const busyRef = useRef(false)
  const creatingSessionRef = useRef(false)
  const runtimeIdByStoredSessionIdRef = useRef(new Map<string, string>())
  const selectedStoredSessionIdRef = useRef<string | null>(null)

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef,
    busyRef,
    creatingSessionRef,
    ensureSessionState: () => createClientSessionState(null),
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway: vi.fn(async () => ({}) as never),
    resetViewSync: vi.fn(),
    routedSessionId: null,
    runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef,
    sessionStateByRuntimeIdRef: states,
    syncSessionStateToView: vi.fn(),
    updateSessionState: (id, updater, storedId) => {
      const next = updater(states.current.get(id) ?? createClientSessionState(storedId ?? null))
      states.current.set(id, next)
      publishSessionState(id, next)

      return next
    }
  })

  useEffect(() => onReady(actions.resumeSession), [actions.resumeSession, onReady])

  return null
}

async function open(row: Partial<SessionInfo>) {
  const primary = { connectionState: 'open', request: vi.fn(async (method: string) => rpcResult(method)) }
  gateways.setPrimaryGateway(primary as never, 'default')
  gateways.setPrimaryGatewayConnection(local)
  setConnection(local)
  setSessions([{ ended_at: null, id: 'stored', is_active: true, message_count: 1, profile: 'default', ...row } as SessionInfo])
  let resume!: Resume
  render(
    <Harness
      onReady={ready => {
        resume = ready
      }}
    />
  )
  await act(() => resume('stored', true))

  return primary.request.mock.calls.map(([method]) => method)
}

describe('opening a kanban worker session', () => {
  beforeEach(() => {
    clearAllSessionStates()
    _resetSessionOwnerHintsForTests()
    $showAllProfiles.set(false)
    $activeGatewayProfile.set('default')
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
    gateways.setPrimaryGateway(null)
    setConnection(null)
    setSessions([])
    $workerSessions.set({})
    $messages.set([])
    clearAllSessionStates()
  })

  it('paints the stored transcript read-only without any session RPC', async () => {
    const methods = await open({ cwd: '/repo/.worktrees/t_ab12', source: 'kanban' })

    expect(methods).toEqual([])
    expect(getLatestSessionMessages).toHaveBeenCalledWith('stored', 'default')
    expect($messages.get()).toHaveLength(1)
    expect($activeSessionId.get()).toBeNull()
    expect($workerSessions.get().stored).toEqual({ state: 'running', taskId: 't_ab12' })
    expect(isStoredTranscriptReadOnly('stored')).toBe(true)
  })

  it('an ordinary session still resumes live', async () => {
    const methods = await open({ source: 'desktop' })

    expect(methods).toContain('session.resume')
    expect($workerSessions.get()).toEqual({})
    expect(isStoredTranscriptReadOnly('stored')).toBe(false)
  })
})
