/**
 * The card drawer's Live tab: one time-ordered feed of what the card's agent
 * is doing — the Hermes worker's commentary and tool calls, card events and
 * comments, heartbeat stage notes, and Claude Code's progress — merged by the
 * backend (`GET /tasks/:id/timeline`). While the card runs it polls with the
 * last page's cursor and appends only what is new; it stops when the card
 * stops running or the tab unmounts.
 */

import { cn, Codicon } from '@hermes/plugin-sdk'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import { getTaskTimeline } from './api'
import { clockTime, mergeTimeline } from './timeline'
import type { TaskTimeline, TimelineEntry, TimelineSource } from './types'
import { errText, useKanban } from './ui'

export const LIVE_POLL_MS = 3000

/** Distance from the bottom (px) that still counts as "following" the feed. */
const STICK_SLACK_PX = 12

const SOURCE_STYLE: Record<TimelineSource, { className: string; icon: string }> = {
  card: { className: 'text-sky-600 dark:text-sky-400', icon: 'note' },
  claude: { className: 'text-amber-600 dark:text-amber-400', icon: 'sparkle' },
  hermes: { className: 'text-violet-600 dark:text-violet-400', icon: 'hubot' },
  stage: { className: 'text-emerald-600 dark:text-emerald-400', icon: 'pulse' }
}

interface Feed {
  entries: TimelineEntry[]
  now: TaskTimeline['now']
}

/** Fetch the feed once, then every {@link LIVE_POLL_MS} while `running`. The
 *  cursor survives a running → stopped flip, so the final fetch only appends. */
function useTimeline(taskId: string, running: boolean) {
  const [feed, setFeed] = useState<Feed>({ entries: [], now: null })
  const [error, setError] = useState<null | string>(null)
  const [loaded, setLoaded] = useState(false)
  const cursor = useRef<string | undefined>(undefined)

  // eslint-disable-next-line no-restricted-syntax -- the ref is the poll's request cursor, advanced by the poll itself; nothing reactive is mirrored
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      try {
        const page = await getTaskTimeline(taskId, cursor.current)

        if (cancelled) {
          return
        }

        cursor.current = page.cursor
        setFeed(prev => ({ entries: mergeTimeline(prev.entries, page.entries), now: page.now }))
        setError(null)
      } catch (e) {
        if (!cancelled) {
          setError(errText(e))
        }
      }

      if (!cancelled) {
        setLoaded(true)

        if (running) {
          timer = setTimeout(() => void tick(), LIVE_POLL_MS)
        }
      }
    }

    void tick()

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [taskId, running])

  return { error, feed, loaded }
}

function LiveRow({ entry }: { entry: TimelineEntry }) {
  const k = useKanban()
  const style = SOURCE_STYLE[entry.source] ?? SOURCE_STYLE.card
  const quiet = entry.kind === 'tool' || entry.kind === 'prompt'

  return (
    <li className="flex items-baseline gap-2" data-source={entry.source} title={entry.detail}>
      <span className="shrink-0 text-(--ui-text-quaternary) tabular-nums">{clockTime(entry.ts)}</span>
      <span className={cn('flex w-16 shrink-0 items-center gap-1 self-center', style.className)}>
        <Codicon name={style.icon} size="0.7rem" />
        <span className="truncate">{k.liveSources[entry.source] ?? entry.source}</span>
      </span>
      <span
        className={cn(
          'min-w-0 break-words whitespace-pre-wrap',
          quiet ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-secondary)'
        )}
      >
        {entry.text}
      </span>
    </li>
  )
}

export function LiveFeed({ running, taskId }: { running: boolean; taskId: string }) {
  const k = useKanban()
  const { error, feed, loaded } = useTimeline(taskId, running)
  const scroller = useRef<HTMLDivElement>(null)
  const following = useRef(true)

  // Follow the newest row only when the reader is already at the bottom.
  useLayoutEffect(() => {
    const el = scroller.current

    if (el && following.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [feed.entries])

  const onScroll = () => {
    const el = scroller.current

    if (el) {
      following.current = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_SLACK_PX
    }
  }

  return (
    <div className="flex flex-col gap-2 font-mono text-[0.6875rem] leading-snug">
      {running && feed.now && (
        <p className="flex items-baseline gap-1.5 text-emerald-600 dark:text-emerald-400" data-testid="live-now">
          <span className="shrink-0 font-medium">{k.liveNow}</span>
          <span className="min-w-0 truncate" title={feed.now.text}>
            {feed.now.text}
          </span>
        </p>
      )}
      {error && <p className="text-destructive">{`${k.liveError}: ${error}`}</p>}
      {loaded && feed.entries.length === 0 && !error && <p className="text-(--ui-text-quaternary)">{k.liveEmpty}</p>}
      <div className="max-h-80 overflow-y-auto" onScroll={onScroll} ref={scroller}>
        <ul className="flex flex-col gap-0.5">
          {feed.entries.map(entry => (
            <LiveRow entry={entry} key={entry.id} />
          ))}
        </ul>
      </div>
    </div>
  )
}
