import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness pins the request scope the task detail is read under.
// eslint-disable-next-line no-restricted-imports
import { setApiRequestConnection } from '@/api/client'
// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'
// Test harness reads the host's toast stack.
// eslint-disable-next-line no-restricted-imports
import { $notifications, clearNotifications } from '@/store/notifications'

import { bindApi } from './api'
import { TaskDrawer } from './drawer'
import { imageMarkdown } from './drawer-comments'
import { en, KANBAN_LOCALES } from './i18n'
import type { KanbanAttachment, KanbanTaskDetail } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const PNG =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='

let detail: KanbanTaskDetail
let client: QueryClient
let disposeApi: () => void
let disposeLocales: () => void
let upload: (filename: string) => Promise<{ attachment: KanbanAttachment | null }>

const rest = vi.fn(async (path: string, options?: PluginRestOptions): Promise<unknown> => {
  if (path === '/tasks/t_img/attachments' && options?.method === 'POST') {
    return upload(options.upload!.filename)
  }

  if (path === '/tasks/t_img') {
    return detail
  }

  if (path.startsWith('/tasks/t_img/log?')) {
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

const uploads = () =>
  rest.mock.calls.filter(([path, options]) => path.endsWith('/attachments') && options?.method === 'POST')

const image = (name: string, type = 'image/png') => new File([new Uint8Array([1, 2, 3])], name, { type })

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  detail = {
    task: { id: 't_img', title: 'Image task', status: 'todo' },
    comments: [],
    events: [],
    attachments: [],
    links: { parents: [], children: [] },
    runs: []
  }
  upload = async filename => ({ attachment: { id: 9, filename, stored_path: `/att/t_img/${filename}` } })
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  disposeApi = bindApi(
    async <T,>(path: string, options?: PluginRestOptions) => (await rest(path, options)) as T,
    { get: (_key, fallback) => fallback, set: vi.fn(), remove: vi.fn() },
    () => vi.fn()
  )
})

afterEach(() => {
  setApiRequestConnection(null)
  clearNotifications()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  cleanup()
  client.clear()
  disposeApi()
  disposeLocales()
  rest.mockClear()
})

const openDrawer = () =>
  render(
    <QueryClientProvider client={client}>
      <TaskDrawer columns={['todo', 'done']} id="t_img" onClose={vi.fn()} onOpen={vi.fn()} />
    </QueryClientProvider>
  )

async function composerAt(text: string, caret: number) {
  const field = (await screen.findByPlaceholderText(en.addComment)) as HTMLTextAreaElement
  fireEvent.change(field, { target: { value: text } })
  field.setSelectionRange(caret, caret)

  return field
}

describe('comment composer images', () => {
  it('uploads a pasted image under a timestamped name and inserts it at the caret', async () => {
    openDrawer()
    const field = await composerAt('see  here', 4)

    fireEvent.paste(field, { clipboardData: { files: [image('image.png')], types: ['Files'] } })

    await waitFor(() => expect(uploads()).toHaveLength(1))
    const filename = uploads()[0][1]!.upload!.filename
    expect(filename).toMatch(/^pasted-\d{14}\.png$/)
    await waitFor(() => expect(field.value).toBe(`see ![${filename}](/att/t_img/${filename}) here`))
    // The sidebar list refetches the detail it reads attachments from.
    await waitFor(() => expect(rest.mock.calls.filter(([path]) => path === '/tasks/t_img').length).toBeGreaterThan(1))
  })

  it('uploads dropped images and leaves non-images alone', async () => {
    openDrawer()
    const field = await composerAt('', 0)

    fireEvent.drop(field.parentElement!, {
      dataTransfer: { files: [image('diagram.jpg', 'image/jpeg'), image('notes.txt', 'text/plain')], types: ['Files'] }
    })

    await waitFor(() => expect(field.value).toBe('![diagram.jpg](/att/t_img/diagram.jpg)'))
    expect(uploads().map(([, options]) => options!.upload!.filename)).toEqual(['diagram.jpg'])
  })

  it('picks images from the attach button and holds Post until the upload lands', async () => {
    let land!: () => void
    upload = filename =>
      new Promise(resolve => {
        land = () => resolve({ attachment: { id: 3, filename, stored_path: `/att/t_img/${filename}` } })
      })

    openDrawer()
    const field = await composerAt('draft', 5)

    const picker = screen
      .getByRole('button', { name: en.attachImage })
      .parentElement!.querySelector('input[type=file]')!

    Object.defineProperty(picker, 'files', { configurable: true, value: [image('shot.png')] })
    fireEvent.change(picker)

    await screen.findByText(en.uploadingImage)
    expect((screen.getByRole('button', { name: en.comment }) as HTMLButtonElement).disabled).toBe(true)

    land()
    await waitFor(() => expect(field.value).toBe('draft![shot.png](/att/t_img/shot.png)'))
    expect(screen.queryByText(en.uploadingImage)).toBeNull()
    expect((screen.getByRole('button', { name: en.comment }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('surfaces an upload failure and leaves the draft untouched', async () => {
    upload = async () => {
      throw new Error('attachment exceeds 25 MB limit')
    }

    openDrawer()
    const field = await composerAt('keep me', 7)

    fireEvent.paste(field, { clipboardData: { files: [image('image.png')], types: ['Files'] } })

    await waitFor(() =>
      expect($notifications.get()[0]).toMatchObject({ kind: 'error', message: 'attachment exceeds 25 MB limit' })
    )
    expect(field.value).toBe('keep me')
    expect(screen.queryByText(en.uploadingImage)).toBeNull()
  })
})

describe('comment images', () => {
  const stored = '/att/t_img/shot (1).png'

  beforeEach(() => {
    setApiRequestConnection('local')
  })

  it("paints a task attachment from its owner's bytes as an object URL, revoked on unmount", async () => {
    const readFileDataUrl = vi.fn().mockResolvedValue(PNG)
    vi.stubGlobal('hermesDesktop', { readFileDataUrl })
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:comment-shot')
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)

    // The modal portals out of the render container; query inside the dialog.
    detail = {
      ...detail,
      attachments: [{ id: 1, filename: 'shot (1).png', stored_path: stored }],
      comments: [{ id: 1, author: 'me', body: `look ${imageMarkdown('shot (1).png', stored)}`, created_at: 0 }]
    }

    const { unmount } = openDrawer()

    await waitFor(() => expect(screen.getByRole('dialog').querySelector('img[src="blob:comment-shot"]')).toBeTruthy())
    expect(readFileDataUrl).toHaveBeenCalledWith(stored)
    expect(createObjectURL).toHaveBeenCalledTimes(1)

    unmount()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:comment-shot')
  })

  it('keeps the default image path for a source that is not a task attachment', async () => {
    const readFileDataUrl = vi.fn().mockResolvedValue(PNG)
    vi.stubGlobal('hermesDesktop', { readFileDataUrl })
    const createObjectURL = vi.spyOn(URL, 'createObjectURL')

    detail = {
      ...detail,
      attachments: [{ id: 1, filename: 'shot.png', stored_path: '/att/t_img/shot.png' }],
      comments: [{ id: 1, author: 'me', body: '![elsewhere](/tmp/elsewhere.png)', created_at: 0 }]
    }

    openDrawer()

    await waitFor(() => expect(screen.getByRole('dialog').querySelector('img')?.getAttribute('src')).toBe(PNG))
    expect(readFileDataUrl).toHaveBeenCalledWith('/tmp/elsewhere.png')
    expect(createObjectURL).not.toHaveBeenCalled()
  })
})
