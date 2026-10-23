// @vitest-environment jsdom
import { AssistantRuntimeProvider, type ThreadMessageLike, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ROUTES_AREA } from '@/app/routes'
import { registry } from '@/contrib/registry'
import { I18nProvider } from '@/i18n'
import { $gateway } from '@/store/gateway'
import { isStoredTranscriptReadOnly } from '@/store/read-only-transcript'
import { $selectedStoredSessionId } from '@/store/session'
import type * as SessionStatesModule from '@/store/session-states'
import { $workerSessions, recordWorkerSession } from '@/store/worker-sessions'

import type { ChatBarState } from './types'
import { WorkerAwareComposerSlot } from './worker-session-notice'

import { ChatBar } from './index'

const { requestForOwnedSession } = vi.hoisted(() => ({
  requestForOwnedSession: vi.fn(async () => ({ control: null }))
}))

// The seam every session-scoped control read goes through (session-control.ts).
vi.mock('@/store/session-states', async importOriginal => ({
  ...(await importOriginal<typeof SessionStatesModule>()),
  requestForOwnedSession
}))

vi.stubGlobal(
  'ResizeObserver',
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
)

const STORED = 'stored-1'
const RUNTIME = 'runtime-1'

const state: ChatBarState = {
  model: { canSwitch: false, model: '', provider: '' },
  tools: { enabled: false, label: '' },
  voice: { enabled: false, active: false }
}

const worker = (overrides: { ended_at?: null | number; is_active?: boolean; cwd?: null | string } = {}) =>
  recordWorkerSession(STORED, {
    cwd: '/repo/.worktrees/t_ab12cd',
    ended_at: null,
    is_active: true,
    source: 'kanban',
    ...overrides
  })

let navigated: string[] = []

function LocationProbe() {
  const location = useLocation()

  if (location.pathname !== '/') {
    navigated = [...navigated, `${location.pathname}${location.search}`]
  }

  return null
}

function Harness({ onSubmit }: { onSubmit: () => Promise<boolean> }) {
  const runtime = useExternalStoreRuntime({
    convertMessage: (message: ThreadMessageLike) => message,
    isRunning: false,
    messages: [] as ThreadMessageLike[],
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <MemoryRouter>
        <I18nProvider configClient={null} initialLocale="en">
          <LocationProbe />
          <WorkerAwareComposerSlot>
            <ChatBar
              busy={false}
              disabled={false}
              gateway={null}
              onCancel={vi.fn()}
              onSubmit={onSubmit}
              sessionId={RUNTIME}
              state={state}
            />
          </WorkerAwareComposerSlot>
        </I18nProvider>
      </MemoryRouter>
    </AssistantRuntimeProvider>
  )
}

const controlReads = () =>
  (requestForOwnedSession.mock.calls as unknown[][]).filter(call => call[2] === 'session.control.read')

beforeEach(() => {
  $gateway.set({ connectionState: 'open', request: vi.fn(async () => ({})) } as never)
  $selectedStoredSessionId.set(STORED)
})

afterEach(() => {
  cleanup()
  $gateway.set(null)
  $selectedStoredSessionId.set(null)
  $workerSessions.set({})
  navigated = []
  vi.clearAllMocks()
})

describe('kanban worker session composer', () => {
  it('a running worker shows the steer-via-card notice instead of a composer, and reads no session controls', () => {
    worker()
    const onSubmit = vi.fn(async () => true)
    const { container } = render(<Harness onSubmit={onSubmit} />)

    const notice = screen.getByText(/comment on the card to steer/)
    // No editor and no send button: there is nothing to type into or submit.
    expect(container.querySelector('[contenteditable]')).toBeNull()
    expect(screen.queryByRole('button', { name: /send/i })).toBeNull()
    fireEvent.keyDown(notice, { key: 'Enter' })
    expect(onSubmit).not.toHaveBeenCalled()
    // Every other send door refuses it too.
    expect(isStoredTranscriptReadOnly(STORED)).toBe(true)
    expect(controlReads()).toEqual([])
  })

  it('a finished worker reads as read-only', () => {
    worker({ ended_at: 1_700_000_000, is_active: false })
    render(<Harness onSubmit={vi.fn(async () => true)} />)

    expect(screen.getByText(/read-only\. Comment on the card to follow up/)).toBeTruthy()
    expect(controlReads()).toEqual([])
  })

  it('offers the card only when the kanban page is mounted, and opens it there', () => {
    worker()
    const { unmount } = render(<Harness onSubmit={vi.fn(async () => true)} />)

    expect(screen.queryByRole('button', { name: 'Open card' })).toBeNull()
    unmount()

    const dispose = registry.register({ area: ROUTES_AREA, data: { path: '/kanban' }, id: 'kanban', render: () => null })

    try {
      render(<Harness onSubmit={vi.fn(async () => true)} />)
      fireEvent.click(screen.getByRole('button', { name: 'Open card' }))
      expect(navigated).toEqual(['/kanban?task=t_ab12cd'])
    } finally {
      dispose()
    }
  })

  it('an ordinary session keeps its composer and its control read', () => {
    const { container } = render(<Harness onSubmit={vi.fn(async () => true)} />)

    expect(screen.queryByText(/Worker session/)).toBeNull()
    expect(container.querySelector('[contenteditable]')).not.toBeNull()
    expect(isStoredTranscriptReadOnly(STORED)).toBe(false)
    expect(controlReads()).toEqual([[RUNTIME, expect.anything(), 'session.control.read', { session_id: RUNTIME }]])
  })
})

describe('worker session row classification', () => {
  it('names the card from a task worktree or scratch workspace, and only there', () => {
    worker({ cwd: '/home/u/.hermes/kanban/boards/main/workspaces/t_99ff' })
    expect($workerSessions.get()[STORED]).toEqual({ state: 'running', taskId: 't_99ff' })

    worker({ cwd: '/Users/u/projects/site', is_active: false })
    expect($workerSessions.get()[STORED]).toEqual({ state: 'finished', taskId: null })
  })
})
