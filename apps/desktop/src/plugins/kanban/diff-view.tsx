/**
 * The Changes view's building blocks: a file's status letter and +/- counts,
 * and a unified diff rendered at full size — monospace, old/new line numbers,
 * add/remove tints from the theme's diff tokens, distinct hunk headers.
 * No syntax highlighting and no split view, on purpose.
 */

import { cn } from '@hermes/plugin-sdk'
import { useMemo } from 'react'

import { type DiffRowKind, parseUnifiedDiff, STATUS_TONE } from './changes'
import type { ChangedFile } from './types'

export function StatusLetter({ status }: { status: ChangedFile['status'] }) {
  return <span className={cn('w-3 shrink-0 text-center font-mono font-semibold', STATUS_TONE[status])}>{status}</span>
}

export function LineCounts({ additions, deletions }: { additions: number; deletions: number }) {
  return (
    <span className="shrink-0 font-mono tabular-nums">
      <span className="text-(--ui-diff-add-foreground)">+{additions}</span>{' '}
      <span className="text-(--ui-diff-remove-foreground)">−{deletions}</span>
    </span>
  )
}

const ROW_CLASS: Record<DiffRowKind, string> = {
  add: 'bg-(--ui-diff-add-background)',
  context: '',
  del: 'bg-(--ui-diff-remove-background)',
  hunk: 'bg-(--ui-bg-quaternary) text-sky-700 dark:text-sky-300',
  meta: 'text-(--ui-text-tertiary) italic',
  note: 'text-(--ui-text-quaternary) italic'
}

const MARKER: Record<DiffRowKind, string> = { add: '+', context: ' ', del: '-', hunk: '', meta: '', note: '' }

const TEXT_CLASS: Record<DiffRowKind, string> = {
  add: 'text-(--ui-diff-add-foreground)',
  context: 'text-(--ui-text-secondary)',
  del: 'text-(--ui-diff-remove-foreground)',
  hunk: '',
  meta: '',
  note: ''
}

const GUTTER = 'w-12 shrink-0 select-none pr-2 text-right text-(--ui-text-quaternary) tabular-nums'

export function DiffView({ patch }: { patch: string }) {
  const rows = useMemo(() => parseUnifiedDiff(patch), [patch])

  return (
    <div className="min-w-max font-mono text-[0.75rem] leading-5" data-testid="diff-view">
      {rows.map((row, index) => (
        <div className={cn('flex whitespace-pre', ROW_CLASS[row.kind])} data-kind={row.kind} key={index}>
          <span className={GUTTER}>{row.oldNo ?? ''}</span>
          <span className={GUTTER}>{row.newNo ?? ''}</span>
          <span className={cn('w-4 shrink-0 select-none text-center', TEXT_CLASS[row.kind])}>{MARKER[row.kind]}</span>
          <span className={cn('pr-4', TEXT_CLASS[row.kind])}>{row.text}</span>
        </div>
      ))}
    </div>
  )
}
