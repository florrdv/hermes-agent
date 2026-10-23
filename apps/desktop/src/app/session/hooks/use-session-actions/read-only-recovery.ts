import { JsonRpcGatewayError } from '@hermes/shared'

import { fetchStoredTranscriptAcrossBackends, getLatestSessionMessages } from '@/hermes'
import { markStoredTranscriptReadOnly } from '@/store/read-only-transcript'
import { isSessionOwnerResolutionError } from '@/store/session-owner-resolution'
import { isWorkerSession, isWorkerSessionRow, recordWorkerSession } from '@/store/worker-sessions'
import type { SessionInfo, SessionMessage, SessionMessagesResponse } from '@/types/hermes'

import { cachedSessionRow } from './utils'

/** `session.resume` error code for a kanban worker's transcript (`kanban_resume_guard.py`). */
const KANBAN_RESUME_REFUSED_CODE = 4132

type RestScope = Parameters<typeof getLatestSessionMessages>[1]

type WorkerRow = Pick<SessionInfo, 'cwd' | 'ended_at' | 'is_active' | 'source'>

export interface WorkerTranscriptOpen {
  isCurrent: () => boolean
  onFailed: () => void
  paintStored: (messages: SessionMessage[]) => void
  restScope: RestScope
  row: WorkerRow
  storedSessionId: string
}

/**
 * Open a kanban worker's transcript (`store/worker-sessions.ts`) from its stored
 * REST page only. No `session.resume` / `session.activate`: the backend refuses
 * a worker's resume, so no runtime is bound and nothing session-scoped (control
 * reads, prompts) is ever dispatched for it.
 */
export async function openWorkerTranscript(open: WorkerTranscriptOpen): Promise<void> {
  recordWorkerSession(open.storedSessionId, open.row)

  try {
    const page = await getLatestSessionMessages(open.storedSessionId, open.restScope)

    if (open.isCurrent()) {
      open.paintStored(page.messages)
    }
  } catch {
    if (open.isCurrent()) {
      open.onFailed()
    }
  }
}

/**
 * A tile's open of a kanban worker's transcript: its stored messages (the
 * tile's own prefetch, else a fresh REST read), or null for an ordinary session
 * that resumes as usual. Classified by the cached row, or by an earlier open.
 */
export async function workerTileTranscript(
  storedSessionId: string,
  prefetch: Promise<null | SessionMessagesResponse>,
  restScope: RestScope
): Promise<null | SessionMessage[]> {
  const row = cachedSessionRow(storedSessionId)

  if (isWorkerSessionRow(row)) {
    recordWorkerSession(storedSessionId, row)
  } else if (!isWorkerSession(storedSessionId)) {
    return null
  }

  const page = (await prefetch) ?? (await getLatestSessionMessages(storedSessionId, restScope).catch(() => null))

  return page?.messages ?? []
}

export interface ReadOnlyRecoveryContext {
  error: unknown
  /** The ambient REST fallback already painted this session's transcript. */
  fallbackPainted: boolean
  isCurrent: () => boolean
  notifyReadOnly: () => void
  paintStored: (messages: SessionMessage[]) => void
  row: undefined | WorkerRow
  storedSessionId: string
}

/**
 * Open a failed `session.resume` as a read-only stored transcript when the
 * failure means "readable, not writable". True when the caller must stop (the
 * transcript opened read-only, or the resume went stale mid-probe).
 *
 * - A kanban refusal (4132) of a row the open did not classify as a worker:
 *   the REST fallback already painted it, so it becomes a worker transcript.
 * - #94724 no-owner recovery: the owner ladder failed closed — CORRECT under
 *   registry topology — but the transcript may be intact in some backend's
 *   state.db. If the ambient fallback didn't paint it, probe the registered
 *   backends READ-ONLY (id-only GET; nothing is routed or minted). Writes stay
 *   blocked until a later resume (after the single-match owner backfill stamps
 *   the row) upgrades it back to a live session.
 */
export async function recoverReadOnlyTranscript(ctx: ReadOnlyRecoveryContext): Promise<boolean> {
  if (
    ctx.error instanceof JsonRpcGatewayError &&
    ctx.error.code === KANBAN_RESUME_REFUSED_CODE &&
    ctx.fallbackPainted
  ) {
    recordWorkerSession(ctx.storedSessionId, { cwd: null, ended_at: null, is_active: false, ...ctx.row, source: 'kanban' })

    return true
  }

  if (!isSessionOwnerResolutionError(ctx.error)) {
    return false
  }

  let painted = ctx.fallbackPainted

  if (!painted) {
    const stored = await fetchStoredTranscriptAcrossBackends(ctx.storedSessionId).catch(() => null)

    if (!ctx.isCurrent()) {
      return true
    }

    if (stored && stored.messages.length > 0) {
      ctx.paintStored(stored.messages)
      painted = true
    }
  }

  if (painted) {
    markStoredTranscriptReadOnly(ctx.storedSessionId)
    ctx.notifyReadOnly()
  }

  return painted
}
