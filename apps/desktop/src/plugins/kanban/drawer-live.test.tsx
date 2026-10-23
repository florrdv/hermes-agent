import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { $boardSlug, bindApi } from './api'
import { TaskDrawer } from './drawer'
import { LIVE_POLL_MS, LiveFeed } from './drawer-live'
import { en, KANBAN_LOCALES } from './i18n'
import type { KanbanTaskDetail, TaskTimeline, TimelineEntry } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const T = 1_792_787_500

const entry = (id: string, ts: number, source: TimelineEntry['source'], text: string, kind = 'said'): TimelineEntry => ({
  id,
  ts,
  source,
  kind,
  text
})

const firstPage: TaskTimeline = {
  task_id: 't_live',
  status: 'running',
  cursor: 'c1',
  now: { ts: T + 20, text: 'claude: running checks' },
  truncated: false,
  entries: [
    entry('e:1', T, 'card', 'claimed by a worker', 'claimed'),
    entry('m:1', T + 5, 'hermes', 'Setting up the worktree.'),
    entry('t:1', T + 15, 'claude', 'Bash npx vitest run src/x', 'tool'),
    entry('e:2', T + 20, 'stage', 'claude: running checks', 'note')
  ]
}

let pages: Record<string, TaskTimeline>
let disposeApi: () => void
let disposeLocales: () => void

const rest = vi.fn(async (path: string, _options?: PluginRestOptions): Promise<unknown> => {
  const [base, qs] = path.split('?')

  if (base === '/tasks/t_live/timeline') {
    const since = new URLSearchParams(qs).get('since') ?? ''

    return pages[since] ?? { ...firstPage, cursor: since, entries: [] }
  }

  if (base === '/tasks/t_live') {
    return detail
  }

  if (base === '/tasks/t_live/log') {
    return { exists: false, content: '', size_bytes: 0, truncated: false }
  }

  if (base === '/profiles') {
    return { profiles: [] }
  }

  if (base === '/orchestration') {
    return { default_assignee: '' }
  }

  throw new Error(`Unexpected REST request: ${path}`)
})

let detail: KanbanTaskDetail

const timelineCalls = () => rest.mock.calls.filter(([path]) => path.split('?')[0].endsWith('/timeline'))

beforeEach(() => {
  rest.mockClear()
  pages = { '': firstPage }
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  disposeApi = bindApi(
    async <T,>(path: string, options?: PluginRestOptions) => (await rest(path, options)) as T,
    { get: (_key, fallback) => fallback, set: vi.fn(), remove: vi.fn() },
    () => vi.fn()
  )
})

afterEach(() => {
  cleanup()
  disposeApi()
  disposeLocales()
  $boardSlug.set('')
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('Live feed', () => {
  it('renders the merged rows in time order with their sources', async () => {
    render(<LiveFeed running={false} taskId="t_live" />)

    await screen.findByText('Setting up the worktree.')
    const rows = screen.getAllByRole('listitem')
    expect(rows.map(row => row.getAttribute('data-source'))).toEqual(['card', 'hermes', 'claude', 'stage'])
    expect(rows[2].textContent).toContain(en.liveSources.claude)
    expect(rows[2].textContent).toContain('Bash npx vitest run src/x')
  })

  it('shows the latest stage note as a "Now:" line while running', async () => {
    render(<LiveFeed running taskId="t_live" />)

    const now = await screen.findByTestId('live-now')
    expect(now.textContent).toBe(`${en.liveNow}claude: running checks`)
  })

  it('polls with the cursor only while running and appends the new entries', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    pages.c1 = { ...firstPage, cursor: 'c2', entries: [entry('t:2', T + 25, 'claude', 'All checks pass.')] }
    const { rerender } = render(<LiveFeed running taskId="t_live" />)

    await screen.findByText('Setting up the worktree.')
    expect(timelineCalls()).toHaveLength(1)

    await act(() => vi.advanceTimersByTimeAsync(LIVE_POLL_MS))
    await screen.findByText('All checks pass.')
    expect(timelineCalls()[1][0]).toBe('/tasks/t_live/timeline?since=c1')
    expect(screen.getAllByRole('listitem')).toHaveLength(5)

    // Stopping fetches once more from the cursor (to catch the completion), then never again.
    rerender(<LiveFeed running={false} taskId="t_live" />)
    await waitFor(() => expect(timelineCalls()).toHaveLength(3))
    expect(timelineCalls()[2][0]).toBe('/tasks/t_live/timeline?since=c2')
    await act(() => vi.advanceTimersByTimeAsync(LIVE_POLL_MS * 3))
    expect(timelineCalls()).toHaveLength(3)
    expect(screen.queryByTestId('live-now')).toBeNull()
  })

  it('stops polling when unmounted', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { unmount } = render(<LiveFeed running taskId="t_live" />)

    await screen.findByText('Setting up the worktree.')
    unmount()
    await act(() => vi.advanceTimersByTimeAsync(LIVE_POLL_MS * 3))
    expect(timelineCalls()).toHaveLength(1)
  })
})

describe('Live tab in the drawer', () => {
  const base: KanbanTaskDetail = {
    task: { id: 't_live', title: 'Live task', status: 'done' },
    comments: [],
    events: [],
    links: { parents: [], children: [] },
    runs: []
  }

  const openDrawer = () =>
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <TaskDrawer columns={['todo', 'done']} id="t_live" onClose={vi.fn()} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

  it('is offered once the card has a run and opens the feed', async () => {
    detail = { ...base, runs: [{ id: 54, profile: 'default', status: 'done' }] }
    openDrawer()

    fireEvent.click(await screen.findByRole('button', { name: en.live }))

    await screen.findByText('Setting up the worktree.')
  })

  it('is hidden before the card has run', async () => {
    detail = { ...base, events: [{ id: 1, kind: 'created', payload: null, created_at: T }] }
    openDrawer()

    await screen.findByText('Live task')
    expect(screen.queryByRole('button', { name: en.live })).toBeNull()
    expect(timelineCalls()).toHaveLength(0)
  })
})
