import { host, type PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { bindApi } from './api'
import { cardChatPrefill } from './chat-about'
import { TaskDrawer } from './drawer'
import { en, KANBAN_LOCALES } from './i18n'
import type { KanbanTaskDetail } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const blocked: KanbanTaskDetail = {
  task: {
    id: 't_example',
    title: 'Example task',
    status: 'blocked',
    block_kind: 'needs_input',
    latest_summary: 'Wired the button; waiting on copy.',
    workspace_path: '/work/t_example'
  },
  comments: [],
  events: [
    { id: 1, kind: 'blocked', payload: { reason: 'stale reason' }, created_at: 1 },
    { id: 2, kind: 'blocked', payload: { reason: 'Which label should the button use?' }, created_at: 2 }
  ],
  links: { parents: [], children: [] },
  runs: []
}

let client: QueryClient
let disposeApi: () => void
let disposeLocales: () => void

const rest = vi.fn(async (path: string, _options?: PluginRestOptions): Promise<unknown> => {
  const [base] = path.split('?')

  if (base === '/tasks/t_example') {
    return blocked
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
  vi.restoreAllMocks()
})

describe('Chat about this', () => {
  it('prefills the card context, ending on a blank line for the question', () => {
    expect(cardChatPrefill(blocked)).toBe(
      [
        'About kanban card t_example: Example task',
        'Status: blocked',
        'Blocked (needs_input): Which label should the button use?',
        'Latest summary: Wired the button; waiting on copy.',
        'Workspace: /work/t_example',
        "For full detail run `hermes kanban show t_example` and read the card's comments.",
        '',
        ''
      ].join('\n')
    )
  })

  it('closes the drawer, opens a new chat and writes the prefill into its composer unsent', async () => {
    const onClose = vi.fn()
    const newChat = vi.spyOn(host, 'newChat').mockImplementation(() => undefined)
    // The fresh composer is not mounted on the first try.
    const setDraft = vi.spyOn(host.composer, 'setDraft').mockResolvedValueOnce(false).mockResolvedValue(true)
    const focus = vi.spyOn(host.composer, 'focus').mockImplementation(() => undefined)
    const submit = vi.spyOn(host.composer, 'submit')

    render(
      <QueryClientProvider client={client}>
        <TaskDrawer columns={['todo', 'blocked']} id="t_example" onClose={onClose} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

    fireEvent.click(await screen.findByRole('button', { name: en.chatAbout }))

    expect(onClose).toHaveBeenCalledOnce()
    expect(newChat).toHaveBeenCalledOnce()
    await waitFor(() => expect(focus).toHaveBeenCalledWith('new'))
    expect(setDraft).toHaveBeenLastCalledWith('new', cardChatPrefill(blocked))
    expect(submit).not.toHaveBeenCalled()
  })
})
