/**
 * Pure helpers behind the Changes tab and its full view: unified-diff parsing
 * with old/new line numbers, the sidebar's directory grouping (which is also
 * the j/k order), and the status-letter tones.
 */

import type { ChangedFile } from './types'

export type DiffRowKind = 'add' | 'context' | 'del' | 'hunk' | 'meta' | 'note'

export interface DiffRow {
  kind: DiffRowKind
  text: string
  oldNo?: number
  newNo?: number
}

const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/

// The file header repeats what the sidebar already shows; mode/rename lines are kept.
const NOISE_PREFIXES = ['diff --git ', 'index ', '--- ', '+++ ']

/** Rows of a single-file unified patch: hunk headers carry their own row,
 *  `+`/`-`/context lines carry new/old line numbers, header noise is dropped. */
export function parseUnifiedDiff(patch: string): DiffRow[] {
  const rows: DiffRow[] = []
  let oldNo = 0
  let newNo = 0
  let inHunk = false

  for (const line of patch.split('\n')) {
    const hunk = HUNK_HEADER.exec(line)

    if (hunk) {
      oldNo = Number(hunk[1])
      newNo = Number(hunk[2])
      inHunk = true
      rows.push({ kind: 'hunk', text: line })
    } else if (line.startsWith('diff --git ')) {
      inHunk = false
    } else if (!inHunk) {
      if (line && !NOISE_PREFIXES.some(prefix => line.startsWith(prefix))) {
        rows.push({ kind: 'meta', text: line })
      }
    } else if (line.startsWith('+')) {
      rows.push({ kind: 'add', text: line.slice(1), newNo: newNo++ })
    } else if (line.startsWith('-')) {
      rows.push({ kind: 'del', text: line.slice(1), oldNo: oldNo++ })
    } else if (line.startsWith('\\')) {
      rows.push({ kind: 'note', text: line })
    } else if (line.startsWith(' ')) {
      rows.push({ kind: 'context', text: line.slice(1), oldNo: oldNo++, newNo: newNo++ })
    }
  }

  return rows
}

export interface FileGroup {
  dir: string
  files: ChangedFile[]
}

const dirOf = (path: string) => (path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '')

/** Files grouped by parent directory, root first, then alphabetical. */
export function groupByDirectory(files: ChangedFile[]): FileGroup[] {
  const groups = new Map<string, ChangedFile[]>()

  for (const file of [...files].sort((a, b) => a.path.localeCompare(b.path))) {
    const dir = dirOf(file.path)
    const group = groups.get(dir)

    if (group) {
      group.push(file)
    } else {
      groups.set(dir, [file])
    }
  }

  // '' (the repo root) sorts before every other directory on its own.
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([dir, list]) => ({ dir, files: list }))
}

/** The files in the order the sidebar shows them — what ↑/↓ and j/k walk. */
export const displayOrder = (files: ChangedFile[]) => groupByDirectory(files).flatMap(group => group.files)

export const baseName = (path: string) => path.slice(path.lastIndexOf('/') + 1)

export const shortSha = (sha: string) => sha.slice(0, 7)

export const STATUS_TONE: Record<ChangedFile['status'], string> = {
  A: 'text-(--ui-diff-add-foreground)',
  D: 'text-(--ui-diff-remove-foreground)',
  M: 'text-amber-600 dark:text-amber-400',
  R: 'text-sky-600 dark:text-sky-400'
}
