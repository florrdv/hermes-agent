/**
 * The card's comment thread pieces: the composer (text plus pasted, dropped or
 * picked images, each uploaded as a task attachment and referenced by its
 * stored path) and the image renderer that paints those references inline.
 */

import { Button, cn, Codicon, host, isSubmitEnter, MarkdownImage, Textarea, Tip } from '@hermes/plugin-sdk'
import {
  type ComponentProps,
  type ComponentType,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState
} from 'react'

import { uploadAttachment } from './api'
import type { KanbanAttachment } from './types'
import { errText, useKanban } from './ui'

export type ReadAttachment = (path: string) => Promise<string>
export type UploadImage = (file: File, filename: string) => Promise<KanbanAttachment | null>

export async function uploadImageAttachment(taskId: string, file: File, filename: string) {
  const { attachment } = await uploadAttachment(taskId, {
    bytes: await file.arrayBuffer(),
    contentType: file.type || undefined,
    filename
  })

  return attachment
}

const imageFiles = (files: FileList | null | undefined) =>
  Array.from(files ?? []).filter(file => file.type.startsWith('image/'))

/** Clipboard images arrive as a generic `image.png`; name them by paste time
 *  so the attachments list (and the worker reading it) can tell them apart. */
export function pastedImageName(file: File, now = new Date(), index?: number): string {
  const stamp = now.toISOString().replace(/\D/g, '').slice(0, 14)
  const ext = file.name.match(/\.[a-z0-9]+$/i)?.[0] ?? `.${file.type.split('/')[1]?.split('+')[0] || 'png'}`

  return `pasted-${stamp}${index === undefined ? '' : `-${index + 1}`}${ext}`
}

// Collision-resolved names carry spaces and parens (`shot (1).png`), which end
// a bare link destination; the `<…>` form takes them. Backslashes (Windows
// paths) are escapes in Markdown, so they are doubled to survive the parse.
export function imageMarkdown(filename: string, path: string): string {
  const alt = filename.replace(/[[\]\\]/g, '\\$&')
  const escaped = path.replace(/\\/g, '\\\\')

  return `![${alt}](${/[\s()<>]/.test(path) ? `<${escaped.replace(/[<>]/g, '\\$&')}>` : escaped})`
}

// Mirrors the review pane's commit-message field: one row tall to start
// (button-height), CSS field-sizing grows it with content, button hugs the
// bottom edge as it grows.
//
// On a RUNNING task the worker polls its comment thread and folds new notes
// into the live turn (OUT-OF-BAND steer), so a plain note reaches the agent
// mid-run within a few seconds — no block/unblock dance. `onRequeue` is the
// heavier option: post the note AND reclaim so the task restarts from scratch
// with the note in context (use when the current run has gone off the rails).
//
// `onUploadImage` (absent on backends without attachments) turns a pasted,
// dropped or picked image into a task attachment and inserts its Markdown at
// the caret; posting waits until every upload has landed.
export function CommentComposer({
  onRequeue,
  onSubmit,
  onUploadImage,
  pending,
  running
}: {
  onRequeue?: (body: string) => void
  onSubmit: (body: string) => void
  onUploadImage?: UploadImage
  pending: boolean
  running?: boolean
}) {
  const k = useKanban()
  const [body, setBody] = useState('')
  const [uploads, setUploads] = useState(0)
  const fieldRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const caretRef = useRef<null | number>(null)

  useLayoutEffect(() => {
    if (caretRef.current !== null) {
      fieldRef.current?.setSelectionRange(caretRef.current, caretRef.current)
      caretRef.current = null
    }
  }, [body])

  const insert = (snippet: string) => {
    const field = fieldRef.current
    const start = field?.selectionStart ?? body.length
    const end = field?.selectionEnd ?? start

    caretRef.current = start + snippet.length
    setBody(prev => prev.slice(0, start) + snippet + prev.slice(end))
  }

  const upload = async (file: File, filename = file.name) => {
    if (!onUploadImage) {
      return
    }

    setUploads(n => n + 1)

    try {
      const attachment = await onUploadImage(file, filename)

      if (attachment?.stored_path) {
        insert(imageMarkdown(attachment.filename, attachment.stored_path))
      }
    } catch (err) {
      host.notify({ kind: 'error', message: errText(err) })
    } finally {
      setUploads(n => n - 1)
    }
  }

  const submit = () => {
    const trimmed = body.trim()

    if (trimmed && !pending && !uploads) {
      onSubmit(trimmed)
      setBody('')
    }
  }

  const requeue = () => {
    const trimmed = body.trim()

    if (trimmed && !pending && !uploads && onRequeue) {
      onRequeue(trimmed)
      setBody('')
    }
  }

  const empty = !body.trim() || pending || uploads > 0
  const sendLabel = running ? k.send : k.comment

  // Sized like the new-project idea field (default control padding, inset icon
  // actions); a labelled text button floating inside the textarea read as part
  // of the input.
  return (
    <div className="flex flex-col gap-1.5">
      <div
        className="relative"
        onDragOver={event => {
          if (onUploadImage && event.dataTransfer.types.includes('Files')) {
            event.preventDefault()
          }
        }}
        onDrop={event => {
          const files = onUploadImage ? imageFiles(event.dataTransfer.files) : []

          if (files.length) {
            event.preventDefault()
            event.stopPropagation()
            files.forEach(file => void upload(file))
          }
        }}
      >
        <Textarea
          className={cn('field-sizing-content max-h-40 resize-none text-[0.8125rem]', onUploadImage ? 'pr-16' : 'pr-9')}
          onChange={event => setBody(event.target.value)}
          onKeyDown={event => {
            if (isSubmitEnter(event) && !event.shiftKey) {
              event.preventDefault()
              submit()
            }
          }}
          onPaste={event => {
            const files = onUploadImage ? imageFiles(event.clipboardData.files) : []

            if (files.length) {
              event.preventDefault()
              const now = new Date()
              files.forEach(
                (file, i) => void upload(file, pastedImageName(file, now, files.length > 1 ? i : undefined))
              )
            }
          }}
          placeholder={running ? k.messageWorker : k.addComment}
          ref={fieldRef}
          value={body}
        />
        <div className="absolute top-1 right-1 flex items-center gap-0.5">
          {onUploadImage && (
            <>
              <input
                accept="image/*"
                hidden
                multiple
                onChange={event => {
                  imageFiles(event.target.files).forEach(file => void upload(file))
                  event.target.value = ''
                }}
                ref={fileRef}
                type="file"
              />
              <Tip label={k.attachImage}>
                <Button
                  aria-label={k.attachImage}
                  className="text-muted-foreground/80 hover:text-foreground"
                  onClick={() => fileRef.current?.click()}
                  size="icon-xs"
                  type="button"
                  variant="ghost"
                >
                  <Codicon name="file-media" size="0.85rem" />
                </Button>
              </Tip>
            </>
          )}
          <Tip label={sendLabel}>
            <Button
              aria-label={sendLabel}
              className="text-muted-foreground/80 hover:text-foreground"
              disabled={empty}
              onClick={submit}
              size="icon-xs"
              type="button"
              variant="ghost"
            >
              <Codicon name="arrow-up" size="0.85rem" />
            </Button>
          </Tip>
        </div>
      </div>
      {uploads > 0 && (
        <span className="flex items-center gap-1 text-[0.625rem] leading-tight text-(--ui-text-quaternary)">
          <Codicon name="sync" size="0.65rem" spinning />
          {k.uploadingImage}
        </span>
      )}
      {running && onRequeue && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-[0.625rem] leading-tight text-(--ui-text-quaternary)">{k.deliveredLive}</span>
          <Button className="shrink-0" disabled={empty} onClick={requeue} size="xs" variant="outline">
            <Codicon name="debug-restart" size="0.7rem" />
            {k.requeueWithNote}
          </Button>
        </div>
      )}
    </div>
  )
}

/** A task attachment painted inline. The path lives on the gateway that
 *  returned the task, so its bytes are read through that backend into an
 *  object URL (revoked on unmount); the core image component then supplies
 *  the bounded frame and click-to-zoom. A failed read falls back to the
 *  default image behaviour for the raw path. */
function AttachmentImage({ alt, path, read }: { alt?: string; path: string; read: ReadAttachment }) {
  const [url, setUrl] = useState('')
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let live = true
    let objectUrl = ''

    setUrl('')
    setFailed(false)
    read(path)
      .then(dataUrl => fetch(dataUrl))
      .then(response => response.blob())
      .then(blob => {
        objectUrl = URL.createObjectURL(blob)

        if (live) {
          setUrl(objectUrl)
        } else {
          URL.revokeObjectURL(objectUrl)
        }
      })
      .catch(() => live && setFailed(true))

    return () => {
      live = false

      if (objectUrl) {
        URL.revokeObjectURL(objectUrl)
      }
    }
  }, [path, read])

  if (failed) {
    return <MarkdownImage alt={alt} src={path} />
  }

  return url ? <MarkdownImage alt={alt} src={url} /> : null
}

const decoded = (src: string) => {
  try {
    return decodeURI(src)
  } catch {
    return src
  }
}

/** The comment thread's `img` component: a source that is one of the task's
 *  attachment paths renders through `AttachmentImage`; anything else keeps the
 *  default. Keyed on the set of paths (not the detail object) so a refetch
 *  doesn't remount every image and re-read its bytes. */
export function useAttachmentImg(
  attachments: KanbanAttachment[] | null | undefined,
  readAttachment: ReadAttachment | undefined
): ComponentType<ComponentProps<'img'>> | undefined {
  const readRef = useRef(readAttachment)
  readRef.current = readAttachment
  const read = useCallback<ReadAttachment>(path => readRef.current!(path), [])

  const paths = (attachments ?? [])
    .map(attachment => attachment.stored_path?.trim())
    .filter(Boolean)
    .join('\n')

  const hasReader = !!readAttachment

  return useMemo(() => {
    if (!paths || !hasReader) {
      return undefined
    }

    const known = new Set(paths.split('\n'))

    return function CommentImage(props: ComponentProps<'img'>) {
      const src = typeof props.src === 'string' ? props.src : ''
      const path = known.has(src) ? src : known.has(decoded(src)) ? decoded(src) : null

      return path ? <AttachmentImage alt={props.alt} path={path} read={read} /> : <MarkdownImage {...props} />
    }
  }, [hasReader, paths, read])
}
