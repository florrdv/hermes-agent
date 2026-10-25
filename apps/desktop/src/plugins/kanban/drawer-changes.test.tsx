import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { $boardSlug, bindApi } from './api'
import { displayOrder, parseUnifiedDiff } from './changes'
import { TaskDrawer } from './drawer'
import { CHANGES_POLL_MS, ChangesTab } from './drawer-changes'
import { en, KANBAN_LOCALES } from './i18n'
import type { ChangedFile, KanbanTaskDetail, TaskChanges } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const file = (path: string, status: ChangedFile['status'], additions: number, deletions: number, extra = {}) => ({
  path,
  status,
  additions,
  deletions,
  binary: false,
  collapsed: false,
  untracked: false,
  ...extra
})

const summary: TaskChanges = {
  task_id: 't_chg',
  status: 'running',
  available: true,
  workspace: '/repo',
  base: { sha: 'aaaaaaa1111', subject: 'before the run', source: 'run_start' },
  head: { sha: 'bbbbbbb2222' },
  commits: [{ sha: 'bbbbbbb2222', subject: 'add feature' }],
  dirty: true,
  totals: { files: 3, additions: 7, deletions: 2 },
  files_truncated: false,
  files: [
    file('src/b.ts', 'M', 3, 2),
    file('README.md', 'A', 4, 0, { untracked: true }),
    file('yarn.lock', 'M', 0, 0, { collapsed: true })
  ]
}

const patchFor = (path: string) => `diff --git a/${path} b/${path}
index 1..2 100644
--- a/${path}
+++ b/${path}
@@ -1,2 +1,2 @@
 keep ${path}
-old ${path}
+new ${path}
`

let changes: TaskChanges
let disposeApi: () => void
let disposeLocales: () => void
let detail: KanbanTaskDetail

const rest = vi.fn(async (path: string, _options?: PluginRestOptions): Promise<unknown> => {
  const [base, qs] = path.split('?')

  if (base === '/tasks/t_chg/changes') {
    const target = new URLSearchParams(qs).get('file')

    return target
      ? {
          task_id: 't_chg',
          status: 'running',
          available: true,
          path: target,
          patch: patchFor(target),
          truncated: false,
          binary: false
        }
      : changes
  }

  if (base === '/tasks/t_chg') {
    return detail
  }

  if (base === '/tasks/t_chg/log') {
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

const summaryCalls = () => rest.mock.calls.filter(([path]) => path === '/tasks/t_chg/changes')

const patchCalls = (target: string) =>
  rest.mock.calls.filter(([path]) => path.endsWith(`file=${encodeURIComponent(target)}`))

beforeEach(() => {
  rest.mockClear()
  changes = summary
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

describe('parseUnifiedDiff', () => {
  it('numbers old/new lines from each hunk header and keeps hunk headers as rows', () => {
    const rows = parseUnifiedDiff(
      'diff --git a/x b/x\nnew file mode 100644\n--- a/x\n+++ b/x\n@@ -3,3 +3,4 @@ fn\n ctx\n-gone\n+one\n+two\n\\ No newline at end of file\n@@ -20 +21 @@\n-a\n+b\n'
    )

    expect(rows.map(r => [r.kind, r.oldNo, r.newNo, r.text])).toEqual([
      ['meta', undefined, undefined, 'new file mode 100644'],
      ['hunk', undefined, undefined, '@@ -3,3 +3,4 @@ fn'],
      ['context', 3, 3, 'ctx'],
      ['del', 4, undefined, 'gone'],
      ['add', undefined, 4, 'one'],
      ['add', undefined, 5, 'two'],
      ['note', undefined, undefined, '\\ No newline at end of file'],
      ['hunk', undefined, undefined, '@@ -20 +21 @@'],
      ['del', 20, undefined, 'a'],
      ['add', undefined, 21, 'b']
    ])
  })

  it('walks files root-first, then by directory', () => {
    expect(displayOrder(summary.available ? summary.files : []).map(f => f.path)).toEqual([
      'README.md',
      'yarn.lock',
      'src/b.ts'
    ])
  })
})

describe('Changes tab', () => {
  it('shows why the workspace cannot be diffed', async () => {
    changes = { task_id: 't_chg', status: 'done', available: false, reason: 'The workspace /x is not a git work tree.' }
    render(<ChangesTab running={false} taskId="t_chg" title="T" />)

    expect((await screen.findByTestId('changes-unavailable')).textContent).toBe(
      'The workspace /x is not a git work tree.'
    )
  })

  it('summarises file count, +/- totals, the dirty badge and the file list', async () => {
    render(<ChangesTab running={false} taskId="t_chg" title="T" />)

    const count = await screen.findByTestId('changes-count')
    expect(count.textContent).toBe(en.changesFiles(3))
    expect(count.parentElement?.textContent).toContain('+7 −2')
    expect(screen.getByText(en.changesDirty)).toBeTruthy()
    expect(screen.getByText('src/b.ts')).toBeTruthy()
    expect(screen.getByText('yarn.lock')).toBeTruthy()
  })

  it('opens the full view on the first file and j/k walk the files, refetching each patch', async () => {
    render(<ChangesTab running={false} taskId="t_chg" title="Refactor parser" />)

    fireEvent.click((await screen.findAllByRole('button', { name: en.changesOpenFull }))[0])
    const view = await screen.findByTestId('changes-full-view')

    expect(screen.getByRole('heading', { name: 'Refactor parser' })).toBeTruthy()
    expect(view.textContent).toContain('aaaaaaa → bbbbbbb')
    expect(view.textContent).toContain('add feature')
    expect(screen.getByTestId('changes-selected-path').textContent).toBe('README.md')
    await screen.findByText('new README.md')

    fireEvent.keyDown(view, { key: 'j' })
    // yarn.lock is collapsed: no patch request until "Show diff".
    expect(screen.getByTestId('changes-selected-path').textContent).toBe('yarn.lock')
    expect(screen.getByText(en.changesCollapsed)).toBeTruthy()
    expect(patchCalls('yarn.lock')).toHaveLength(0)

    fireEvent.keyDown(view, { key: 'ArrowDown' })
    expect(screen.getByTestId('changes-selected-path').textContent).toBe('src/b.ts')
    await screen.findByText('new src/b.ts')
    const added = screen.getByText('new src/b.ts').closest('[data-kind]')
    expect(added?.getAttribute('data-kind')).toBe('add')

    fireEvent.keyDown(view, { key: 'j' }) // already last: stays
    expect(screen.getByTestId('changes-selected-path').textContent).toBe('src/b.ts')

    fireEvent.keyDown(view, { key: 'k' })
    fireEvent.click(screen.getByRole('button', { name: en.changesShowDiff }))
    await screen.findByText('new yarn.lock')
  })

  it('polls while running, keeps the selected file and refetches its patch', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<ChangesTab running taskId="t_chg" title="T" />)

    fireEvent.click(await screen.findByText('src/b.ts'))
    await screen.findByText('new src/b.ts')
    expect(screen.getByTestId('changes-live')).toBeTruthy()
    expect(patchCalls('src/b.ts')).toHaveLength(1)

    changes = { ...summary, files: [file('a.ts', 'A', 1, 0), ...(summary.available ? summary.files : [])] }
    await act(() => vi.advanceTimersByTimeAsync(CHANGES_POLL_MS))

    await waitFor(() => expect(summaryCalls()).toHaveLength(2))
    await waitFor(() => expect(patchCalls('src/b.ts')).toHaveLength(2))
    expect(screen.getByTestId('changes-selected-path').textContent).toBe('src/b.ts')
  })
})

describe('Changes tab in the drawer', () => {
  const base: KanbanTaskDetail = {
    task: { id: 't_chg', title: 'Changes task', status: 'done' },
    comments: [],
    events: [],
    links: { parents: [], children: [] },
    runs: []
  }

  const openDrawer = () =>
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <TaskDrawer columns={['todo', 'done']} id="t_chg" onClose={vi.fn()} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

  it('is offered when the task has a workspace and is hidden otherwise', async () => {
    detail = { ...base, task: { ...base.task, workspace_path: '/repo' } }
    const { unmount } = openDrawer()

    fireEvent.click(await screen.findByRole('button', { name: en.changes }))
    await screen.findByTestId('changes-count')
    unmount()

    detail = base
    openDrawer()
    await screen.findByText('Changes task')
    expect(screen.queryByRole('button', { name: en.changes })).toBeNull()
  })
})
