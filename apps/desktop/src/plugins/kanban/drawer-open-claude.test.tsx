import { host, type PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { $boardSlug, bindApi } from './api'
import { TaskDrawer } from './drawer'
import { en, KANBAN_LOCALES } from './i18n'
import type { KanbanTaskDetail, OpenClaudeSessionResult } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const baseDetail: KanbanTaskDetail = {
  task: { id: 't_example', title: 'Example task', status: 'done' },
  comments: [],
  events: [],
  links: { parents: [], children: [] },
  runs: []
}

const resumed: OpenClaudeSessionResult = {
  task_id: 't_example',
  mode: 'resume',
  argv: ['claude', '--resume', 'sess-1'],
  cwd: '/work/t_example',
  reason: 'no live tmux session; resuming Claude session from run 41 in recorded cwd /work/t_example',
  command: 'claude --resume sess-1',
  shell_line: 'cd /work/t_example && claude --resume sess-1',
  launched: true,
  error: null,
  launch_config: '/tmp/warp/launch.yaml'
}

let detail: KanbanTaskDetail
let openResult: OpenClaudeSessionResult
let client: QueryClient
let disposeApi: () => void
let disposeLocales: () => void

const rest = vi.fn(async (path: string, _options?: PluginRestOptions): Promise<unknown> => {
  const [base] = path.split('?')

  if (base === '/tasks/t_example') {
    return detail
  }

  if (base === '/tasks/t_example/open-session') {
    return openResult
  }

  if (base === '/tasks/t_example/log') {
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

beforeEach(() => {
  rest.mockClear()
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  disposeApi = bindApi(
    async <T,>(path: string, options?: PluginRestOptions) => (await rest(path, options)) as T,
    { get: (_key, fallback) => fallback, set: vi.fn(), remove: vi.fn() },
    () => vi.fn()
  )
})

afterEach(() => {
  cleanup()
  client.clear()
  disposeApi()
  disposeLocales()
  $boardSlug.set('')
  vi.restoreAllMocks()
})

function openDrawer() {
  render(
    <QueryClientProvider client={client}>
      <TaskDrawer columns={['todo', 'done']} id="t_example" onClose={vi.fn()} onOpen={vi.fn()} />
    </QueryClientProvider>
  )
}

async function enabledButton() {
  const button = await screen.findByRole('button', { name: en.openClaude })
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))

  return button
}

const openSessionCalls = () => rest.mock.calls.filter(([path]) => path.split('?')[0].endsWith('/open-session'))

describe('Open Claude Code', () => {
  it('is disabled while the card has never run', async () => {
    detail = baseDetail
    openDrawer()

    const button = await screen.findByRole('button', { name: en.openClaude })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(button)
    expect(openSessionCalls()).toHaveLength(0)
  })

  it("POSTs the card's open-session endpoint on the selected board and toasts the resume", async () => {
    const notify = vi.spyOn(host, 'notify')

    $boardSlug.set('side-project')
    detail = { ...baseDetail, runs: [{ id: 41, profile: 'claude-coder', status: 'done' }] }
    openResult = resumed
    openDrawer()

    fireEvent.click(await enabledButton())

    await waitFor(() => expect(openSessionCalls()).toHaveLength(1))
    const [path, options] = openSessionCalls()[0]
    expect(path).toBe('/tasks/t_example/open-session?board=side-project')
    expect(options?.method).toBe('POST')
    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'success', message: en.claudeResumed }))
    )
  })

  it('surfaces the error and the command to run when Warp could not be launched', async () => {
    const notify = vi.spyOn(host, 'notify')

    detail = { ...baseDetail, runs: [{ id: 41, profile: 'claude-coder', status: 'done' }] }
    openResult = { ...resumed, launched: false, error: 'Warp is not installed', launch_config: null }
    openDrawer()

    fireEvent.click(await enabledButton())

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: 'error',
          message: 'Warp is not installed',
          detail: en.claudeRunItYourself('cd /work/t_example && claude --resume sess-1')
        })
      )
    )
  })
})
