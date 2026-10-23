import { useStore } from '@nanostores/react'

import { useSessionView } from '@/app/chat/session-view'
import { useStoreSelector } from '@/lib/use-session-slice'
import { isWatchWindow } from '@/store/windows'
import { $workerSessions, type WorkerSession } from '@/store/worker-sessions'

/** The kanban worker session this surface shows, or null for an ordinary chat. */
export function useWorkerSession(): null | WorkerSession {
  const storedId = useStore(useSessionView().$storedId)

  return useStoreSelector($workerSessions, sessions => (storedId ? (sessions[storedId] ?? null) : null))
}

/** Prompts here can't be edited, restored or stopped: a watch window spectates a
 *  subagent driven elsewhere, and a kanban worker is steered from its card. */
export function useSpectatorTranscript(): boolean {
  return useWorkerSession() !== null || isWatchWindow()
}

/** The transcript takes no reload/retry: a history page, or a kanban worker's. */
export function useStaticTranscript(isHistorical: boolean): boolean {
  return useWorkerSession() !== null || isHistorical
}
