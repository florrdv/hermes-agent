import { host, type PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The sidebar's session-open door, observed to prove the card reaches it.
// eslint-disable-next-line no-restricted-imports
import { openSession as openSessionDoor } from '@/app/open-session'
// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { bindApi } from './api'
import { TaskDrawer } from './drawer'
import { en, KANBAN_LOCALES } from './i18n'
import type { KanbanTaskDetail } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))
vi.mock('@/app/open-session', async importOriginal => ({
  ...(await importOriginal<object>()),
  openSession: vi.fn()
}))

const baseDetail: KanbanTaskDetail = {
  task: { id: 't_example', title: 'Example task', status: 'done' },
  comments: [],
  events: [],
  links: { parents: [], children: [] },
  runs: []
}

let detail: KanbanTaskDetail
let client: QueryClient
let disposeApi: () => void
let disposeLocales: () => void

const rest = vi.fn(async (path: string): Promise<unknown> => {
  if (path === '/tasks/t_example') {
    return detail
  }

  if (path.startsWith('/tasks/t_example/log?')) {
    return { exists: false, content: '', size_bytes: 0, truncated: false }
  }

  if (path === '/profiles') {
    return { profiles: [] }
  }

  if (path === '/orchestration') {
    return { default_assignee: '' }
  }

  throw new Error(`Unexpected REST request: ${path}`)
})

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  disposeApi = bindApi(
    async <T,>(path: string, _options?: PluginRestOptions) => (await rest(path)) as T,
    { get: (_key, fallback) => fallback, set: vi.fn(), remove: vi.fn() },
    () => vi.fn()
  )
})

afterEach(() => {
  cleanup()
  client.clear()
  disposeApi()
  disposeLocales()
  vi.restoreAllMocks()
})

function openDrawer(onClose = vi.fn()) {
  render(
    <QueryClientProvider client={client}>
      <TaskDrawer columns={['todo', 'done']} id="t_example" onClose={onClose} onOpen={vi.fn()} />
    </QueryClientProvider>
  )

  return onClose
}

describe('Go to chat', () => {
  it("opens the latest run's worker session through the sidebar's open door and closes the card", async () => {
    const activeProfile = host.state.profile.get()

    detail = {
      ...baseDetail,
      events: [
        { id: 2, kind: 'owner_admitted', payload: { session_id: 'local-first' }, created_at: 2, run_id: 40 },
        { id: 5, kind: 'owner_admitted', payload: { session_id: 'local-latest' }, created_at: 5, run_id: 41 }
      ],
      runs: [
        { id: 40, profile: activeProfile, status: 'done' },
        { id: 41, profile: activeProfile, status: 'done' }
      ]
    }
    const onClose = openDrawer()

    const button = await screen.findByRole('button', { name: en.goToChat })
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(button)

    // Same profile as the one in front: a plain in-place open, exactly a sidebar click.
    await waitFor(() => expect(openSessionDoor).toHaveBeenCalledWith('local-latest', expect.any(Function), 'in-place'))
    expect(onClose).toHaveBeenCalled()
  })

  it("routes to the worker's profile when it is not the one in front", async () => {
    const openSession = vi.spyOn(host, 'openSession').mockResolvedValue()

    detail = {
      ...baseDetail,
      events: [{ id: 5, kind: 'owner_admitted', payload: { session_id: 'local-coder' }, created_at: 5, run_id: 41 }],
      runs: [{ id: 41, profile: 'kanban-coder-elsewhere', status: 'done' }]
    }
    openDrawer()

    const button = await screen.findByRole('button', { name: en.goToChat })
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(button)

    expect(openSession).toHaveBeenCalledWith('local-coder', { profile: 'kanban-coder-elsewhere' })
  })

  it('is disabled while the card has no run with a session', async () => {
    const openSession = vi.spyOn(host, 'openSession').mockResolvedValue()

    detail = { ...baseDetail, runs: [{ id: 41, profile: 'coder', status: 'running' }] }
    openDrawer()

    const button = await screen.findByRole('button', { name: en.goToChat })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(button)
    expect(openSession).not.toHaveBeenCalled()
  })
})
