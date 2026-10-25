/**
 * The Changes full view: a near-full-window modal over the card drawer. Left:
 * commits since the base and the changed files grouped by directory; right:
 * the selected file's unified diff. ↑/↓ or j/k walk the files, Esc closes
 * (the shared Dialog closes only the topmost layer, so the drawer stays).
 * The selected file's patch is refetched whenever the summary refreshes, so
 * it follows a running task; lockfiles wait for "Show diff", binaries never load.
 */

import { Badge, Button, cn, Codicon, Dialog, DialogContent, DialogTitle, Loader } from '@hermes/plugin-sdk'
import { type KeyboardEvent, useEffect, useRef, useState } from 'react'

import { getTaskFilePatch } from './api'
import { baseName, displayOrder, groupByDirectory, shortSha } from './changes'
import { DiffView, LineCounts, StatusLetter } from './diff-view'
import type { ChangedFile, FilePatch, TaskChanges } from './types'
import { errText, useKanban } from './ui'

export type ChangesSummary = Extract<TaskChanges, { available: true }>

const STEP_KEYS: Record<string, number> = { ArrowDown: 1, ArrowUp: -1, j: 1, k: -1 }

interface PatchState {
  path: string
  patch: FilePatch | null
  error: null | string
}

/** The selected file's patch, refetched whenever `version` (the summary) changes.
 *  The previous patch of the same path stays up while a refresh is in flight. */
function useFilePatch(taskId: string, path: null | string, version: unknown) {
  const [state, setState] = useState<PatchState | null>(null)

  useEffect(() => {
    if (!path) {
      return
    }

    let cancelled = false

    getTaskFilePatch(taskId, path).then(
      patch => !cancelled && setState({ path, patch, error: null }),
      (e: unknown) => !cancelled && setState({ path, patch: null, error: errText(e) })
    )

    return () => {
      cancelled = true
    }
  }, [taskId, path, version])

  return state?.path === path ? state : null
}

function FileButton({ file, onSelect, selected }: { file: ChangedFile; onSelect: () => void; selected: boolean }) {
  const k = useKanban()
  const ref = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (selected) {
      // jsdom has no scrollIntoView.
      ref.current?.scrollIntoView?.({ block: 'nearest' })
    }
  }, [selected])

  const title = [file.old_path ? `${file.old_path} → ${file.path}` : file.path, file.untracked && k.changesUntracked]
    .filter(Boolean)
    .join(' · ')

  return (
    <button
      aria-current={selected || undefined}
      className={cn(
        'flex w-full items-center gap-1.5 rounded px-2 py-0.5 text-left text-[0.75rem]',
        selected ? 'bg-(--ui-control-active-background) text-foreground' : 'hover:bg-(--ui-bg-quaternary)'
      )}
      data-path={file.path}
      onClick={onSelect}
      ref={ref}
      title={title}
      type="button"
    >
      <StatusLetter status={file.status} />
      <span className="min-w-0 flex-1 truncate">{baseName(file.path)}</span>
      {file.collapsed && <Codicon className="text-(--ui-text-quaternary)" name="fold" size="0.7rem" />}
      {file.binary ? (
        <span className="text-[0.625rem] text-(--ui-text-quaternary)">bin</span>
      ) : (
        <LineCounts additions={file.additions} deletions={file.deletions} />
      )}
    </button>
  )
}

function Sidebar({
  changes,
  onSelect,
  selected
}: {
  changes: ChangesSummary
  onSelect: (path: string) => void
  selected: null | string
}) {
  const k = useKanban()

  return (
    <aside className="flex w-80 shrink-0 flex-col gap-4 overflow-y-auto border-r border-(--ui-stroke-tertiary) p-3">
      {changes.commits.length > 0 && (
        <section className="flex flex-col gap-1">
          <h3 className="text-[0.6875rem] font-medium text-(--ui-text-tertiary)">
            {k.changesCommits(changes.commits.length)}
          </h3>
          <ul className="flex flex-col gap-0.5 text-[0.6875rem]">
            {changes.commits.map(commit => (
              <li className="flex items-baseline gap-2" key={commit.sha} title={commit.subject}>
                <span className="shrink-0 font-mono text-(--ui-text-quaternary)">{shortSha(commit.sha)}</span>
                <span className="min-w-0 truncate text-(--ui-text-secondary)">{commit.subject}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className="flex flex-col gap-1">
        <h3 className="flex items-baseline gap-2 text-[0.6875rem] font-medium text-(--ui-text-tertiary)">
          {k.changesFilesHeading}
          <span className="font-normal">{k.changesFiles(changes.totals.files)}</span>
          <LineCounts additions={changes.totals.additions} deletions={changes.totals.deletions} />
        </h3>
        {groupByDirectory(changes.files).map(group => (
          <div className="flex flex-col" key={group.dir}>
            {group.dir && (
              <span
                className="truncate px-2 pt-1.5 font-mono text-[0.625rem] text-(--ui-text-quaternary)"
                title={group.dir}
              >
                {group.dir}/
              </span>
            )}
            {group.files.map(file => (
              <FileButton
                file={file}
                key={file.path}
                onSelect={() => onSelect(file.path)}
                selected={file.path === selected}
              />
            ))}
          </div>
        ))}
        {changes.files_truncated && (
          <p className="text-[0.625rem] text-(--ui-text-quaternary)">{k.changesFilesTruncated}</p>
        )}
      </section>
    </aside>
  )
}

function Notice({ children }: { children: string }) {
  return <p className="p-6 text-[0.75rem] text-(--ui-text-tertiary)">{children}</p>
}

function FileDiff({ file, taskId, version }: { file: ChangedFile; taskId: string; version: unknown }) {
  const k = useKanban()
  const [revealed, setRevealed] = useState(false)
  const wanted = !file.binary && (!file.collapsed || revealed)
  const state = useFilePatch(taskId, wanted ? file.path : null, version)

  if (file.binary) {
    return <Notice>{k.changesBinary}</Notice>
  }

  if (!wanted) {
    return (
      <div className="flex flex-col items-start gap-2 p-6 text-[0.75rem] text-(--ui-text-tertiary)">
        <p>{k.changesCollapsed}</p>
        <Button onClick={() => setRevealed(true)} size="sm" variant="outline">
          {k.changesShowDiff}
        </Button>
      </div>
    )
  }

  if (state?.error) {
    return <Notice>{`${k.changesError}: ${state.error}`}</Notice>
  }

  if (!state?.patch) {
    return (
      <div className="grid place-items-center p-10">
        <Loader type="lemniscate-bloom" />
      </div>
    )
  }

  if (!state.patch.available) {
    return <Notice>{state.patch.reason}</Notice>
  }

  return (
    <>
      {state.patch.truncated && (
        <p className="border-b border-(--ui-stroke-tertiary) bg-amber-500/10 px-4 py-1.5 text-[0.6875rem] text-amber-700 dark:text-amber-300">
          {k.changesTruncated}
        </p>
      )}
      <DiffView patch={state.patch.patch} />
    </>
  )
}

export function ChangesFullView({
  changes,
  initialPath,
  onClose,
  running,
  taskId,
  title
}: {
  changes: ChangesSummary
  initialPath?: string
  onClose: () => void
  running: boolean
  taskId: string
  title: string
}) {
  const k = useKanban()
  const ordered = displayOrder(changes.files)
  const [selectedPath, setSelectedPath] = useState<null | string>(initialPath ?? null)
  // Selection survives refreshes by path; a file that left the list falls back to the first.
  const selected = ordered.find(f => f.path === selectedPath) ?? ordered[0]

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.metaKey || event.ctrlKey || event.altKey || !selected) {
      return
    }

    const step = STEP_KEYS[event.key]

    if (step) {
      event.preventDefault()
      const index = Math.min(Math.max(ordered.indexOf(selected) + step, 0), ordered.length - 1)
      setSelectedPath(ordered[index].path)
    }
  }

  return (
    <Dialog onOpenChange={open => !open && onClose()} open>
      <DialogContent
        aria-describedby={undefined}
        bodyClassName="flex h-full flex-col gap-0 overflow-hidden p-0"
        className="h-[92vh] max-h-none w-[95vw] max-w-none"
        data-testid="changes-full-view"
        onKeyDown={onKeyDown}
      >
        <header className="flex flex-col gap-1 border-b border-(--ui-stroke-tertiary) px-5 pt-4 pb-3 pr-12">
          <DialogTitle className="truncate leading-snug">{title}</DialogTitle>
          <div className="flex flex-wrap items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)">
            <span className="font-mono" title={changes.base.subject}>
              {`${shortSha(changes.base.sha)} → ${shortSha(changes.head.sha)}`}
            </span>
            {changes.dirty && (
              <Badge size="xs" variant="warn">
                {k.changesDirty}
              </Badge>
            )}
            {running && (
              <span
                className="flex items-center gap-1 text-emerald-600 dark:text-emerald-400"
                data-testid="changes-live"
              >
                <span className="size-1.5 animate-pulse rounded-full bg-current" />
                {k.changesLive}
              </span>
            )}
            <span className="ml-auto text-(--ui-text-quaternary)">{k.changesKeysHint}</span>
          </div>
        </header>
        <div className="flex min-h-0 flex-1">
          <Sidebar changes={changes} onSelect={setSelectedPath} selected={selected?.path ?? null} />
          <main className="min-w-0 flex-1 overflow-auto">
            {selected ? (
              <>
                <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background) px-4 py-2 text-[0.75rem]">
                  <StatusLetter status={selected.status} />
                  <span className="min-w-0 truncate font-mono" data-testid="changes-selected-path">
                    {selected.old_path ? `${selected.old_path} → ${selected.path}` : selected.path}
                  </span>
                  {!selected.binary && <LineCounts additions={selected.additions} deletions={selected.deletions} />}
                </div>
                <FileDiff file={selected} key={selected.path} taskId={taskId} version={changes} />
              </>
            ) : (
              <Notice>{changes.files.length ? k.changesSelectFile : k.changesEmpty}</Notice>
            )}
          </main>
        </div>
      </DialogContent>
    </Dialog>
  )
}
