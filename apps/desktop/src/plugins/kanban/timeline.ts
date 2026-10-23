import type { TimelineEntry } from './types'

/** Rows kept in the drawer; the backend's first page is already capped at 300. */
export const TIMELINE_MAX_ROWS = 1000

/** Fold a page of entries into the feed: dedupe on `id`, keep time order. A
 *  late row (a worker message persisted after newer Claude lines) lands at its
 *  timestamp; equal timestamps keep arrival order (the sort is stable). */
export function mergeTimeline(prev: TimelineEntry[], page: TimelineEntry[]): TimelineEntry[] {
  if (page.length === 0) {
    return prev
  }

  const seen = new Set(prev.map(entry => entry.id))
  const fresh = page.filter(entry => !seen.has(entry.id))

  if (fresh.length === 0) {
    return prev
  }

  const merged = [...prev, ...fresh].sort((a, b) => a.ts - b.ts)

  return merged.length > TIMELINE_MAX_ROWS ? merged.slice(-TIMELINE_MAX_ROWS) : merged
}

/** `HH:MM` in the viewer's locale and timezone, always 24h-wide. */
export function clockTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', hour12: false, minute: '2-digit' })
}
