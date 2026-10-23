import type * as React from 'react'
import { Suspense } from 'react'
import { useNavigate } from 'react-router'

import { contributedRoutes, navigateToWorkspacePage, ROUTES_AREA } from '@/app/routes'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useContributions } from '@/contrib/react/use-contributions'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import type { WorkerSession } from '@/store/worker-sessions'

import { useWorkerSession } from '../use-worker-session'

import { ChatBarFallback } from './index'

// The kanban plugin's board page; it opens the card named by `?task=`.
const KANBAN_ROUTE = '/kanban'

interface WorkerSessionNoticeProps {
  session: WorkerSession
}

interface WorkerAwareComposerSlotProps {
  children: React.ReactNode
}

// Stands in for the composer on a kanban worker's transcript. A worker is
// steered through its card (comments, block/unblock), never by typing into the
// session, so there is no input here at all.
export function WorkerSessionNotice({ session }: WorkerSessionNoticeProps) {
  const { t } = useI18n()
  const navigate = useNavigate()
  const kanbanMounted = contributedRoutes(useContributions(ROUTES_AREA)).some(route => route.path === KANBAN_ROUTE)
  const taskId = kanbanMounted ? session.taskId : null

  return (
    <div
      className={cn(
        'group/composer absolute bottom-0 left-1/2 z-30 w-[min(var(--composer-width),calc(100%-2rem))] max-w-full -translate-x-1/2 rounded-2xl pt-2 pb-[var(--composer-shell-pad-block-end)]',
        'bg-linear-to-b from-transparent to-background/55'
      )}
      data-slot="composer-root"
    >
      <div
        className="flex min-h-11 w-full items-center gap-2 rounded-[inherit] border border-(--dt-input) bg-(--ui-sidebar-surface-background) px-3.5 text-[0.8125rem] text-(--ui-text-secondary)"
        data-state={session.state}
        role="status"
      >
        <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="lock" size="0.875rem" />
        <span className="min-w-0 flex-1 truncate">
          {session.state === 'running' ? t.desktop.workerSessionLive : t.desktop.workerSessionFinished}
        </span>
        {taskId && (
          <Button
            className="shrink-0"
            onClick={() => navigateToWorkspacePage(navigate, `${KANBAN_ROUTE}?task=${encodeURIComponent(taskId)}`)}
            size="xs"
            variant="outline"
          >
            {t.desktop.workerSessionOpenCard}
          </Button>
        )}
      </div>
    </div>
  )
}

// The composer slot (and its Suspense boundary): a kanban worker's transcript
// gets the notice, every other session its normal composer (`children`).
export function WorkerAwareComposerSlot({ children }: WorkerAwareComposerSlotProps) {
  const session = useWorkerSession()

  return session ? (
    <WorkerSessionNotice session={session} />
  ) : (
    <Suspense fallback={<ChatBarFallback />}>{children}</Suspense>
  )
}
