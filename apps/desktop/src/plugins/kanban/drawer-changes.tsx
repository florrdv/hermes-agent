/**
 * The card drawer's Changes tab: the task workspace's code changes since the
 * commit its first run started from (`GET /tasks/:id/changes`) — committed,
 * uncommitted and untracked. A compact summary here; "Open full view" (or a
 * file row) opens the full-size diff. Polls while the card runs, like the Live
 * tab, and stops on the final state when it stops.
 */

import { Badge, Button, Codicon, Tip } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import { getTaskChanges } from './api'
import { ChangesFullView } from './changes-full-view'
import { LineCounts, StatusLetter } from './diff-view'
import type { TaskChanges } from './types'
import { errText, useKanban } from './ui'

export const CHANGES_POLL_MS = 3000

/** Fetch once, then every {@link CHANGES_POLL_MS} while `running`. */
function useTaskChanges(taskId: string, running: boolean) {
  const [changes, setChanges] = useState<null | TaskChanges>(null)
  const [error, setError] = useState<null | string>(null)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      try {
        const next = await getTaskChanges(taskId)

        if (!cancelled) {
          setChanges(next)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) {
          setError(errText(e))
        }
      }

      if (!cancelled && running) {
        timer = setTimeout(() => void tick(), CHANGES_POLL_MS)
      }
    }

    void tick()

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [taskId, running])

  return { changes, error }
}

export function ChangesTab({ running, taskId, title }: { running: boolean; taskId: string; title: string }) {
  const k = useKanban()
  const { changes, error } = useTaskChanges(taskId, running)
  // undefined = closed; '' = open on the first file.
  const [fullView, setFullView] = useState<string | undefined>(undefined)

  if (error && !changes) {
    return <p className="text-[0.6875rem] text-destructive">{`${k.changesError}: ${error}`}</p>
  }

  if (!changes) {
    return null
  }

  if (!changes.available) {
    return (
      <p className="text-[0.6875rem] text-(--ui-text-tertiary)" data-testid="changes-unavailable">
        {changes.reason}
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-2 text-[0.6875rem]">
      <div className="flex items-center gap-2">
        <span className="font-medium text-(--ui-text-secondary)" data-testid="changes-count">
          {k.changesFiles(changes.totals.files)}
        </span>
        <LineCounts additions={changes.totals.additions} deletions={changes.totals.deletions} />
        {changes.dirty && (
          <Badge size="xs" variant="warn">
            {k.changesDirty}
          </Badge>
        )}
        <div className="ml-auto flex items-center gap-0.5">
          <Button onClick={() => setFullView('')} size="xs" variant="ghost">
            {k.changesOpenFull}
          </Button>
          <Tip label={k.changesOpenFull}>
            <Button aria-label={k.changesOpenFull} onClick={() => setFullView('')} size="icon-xs" variant="ghost">
              <Codicon name="screen-full" size="0.8rem" />
            </Button>
          </Tip>
        </div>
      </div>
      {error && <p className="text-destructive">{`${k.changesError}: ${error}`}</p>}
      {changes.files.length === 0 ? (
        <p className="text-(--ui-text-quaternary)">{k.changesEmpty}</p>
      ) : (
        <ul className="flex max-h-80 flex-col overflow-y-auto">
          {changes.files.map(file => (
            <li key={file.path}>
              <button
                className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-(--ui-bg-quaternary)"
                onClick={() => setFullView(file.path)}
                title={file.old_path ? `${file.old_path} → ${file.path}` : file.path}
                type="button"
              >
                <StatusLetter status={file.status} />
                <span className="min-w-0 flex-1 truncate font-mono text-(--ui-text-secondary)">{file.path}</span>
                {file.binary ? (
                  <span className="text-(--ui-text-quaternary)">bin</span>
                ) : (
                  <LineCounts additions={file.additions} deletions={file.deletions} />
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
      {fullView !== undefined && (
        <ChangesFullView
          changes={changes}
          initialPath={fullView || undefined}
          onClose={() => setFullView(undefined)}
          running={running}
          taskId={taskId}
          title={title}
        />
      )}
    </div>
  )
}
