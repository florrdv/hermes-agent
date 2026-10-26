import { Button, Codicon, host, Tip } from '@hermes/plugin-sdk'

import { useKanban } from './i18n'
import type { KanbanTaskDetail } from './types'

// The fresh chat's composer mounts a beat after the route change, and the draft
// bus only answers a mounted surface — poll until it claims the prefill.
const PREFILL_ATTEMPTS = 30
const PREFILL_RETRY_MS = 100

const latestBlockReason = (detail: KanbanTaskDetail): string => {
  const event = detail.events.findLast(ev => ev.kind === 'blocked')
  const reason = (event?.payload as { reason?: unknown } | null | undefined)?.reason

  return typeof reason === 'string' ? reason.trim() : ''
}

/** The context block a "Chat about this" draft opens with. Agent-facing, so
 *  not localized; ends on a blank line so the caret lands where the question
 *  goes. */
export function cardChatPrefill(detail: KanbanTaskDetail): string {
  const { task } = detail
  const lines = [`About kanban card ${task.id}: ${task.title || '(untitled)'}`, `Status: ${task.status}`]

  if (task.status === 'blocked') {
    const reason = latestBlockReason(detail)

    if (task.block_kind || reason) {
      lines.push(`Blocked (${task.block_kind || 'untyped'})${reason ? `: ${reason}` : ''}`)
    }
  }

  if (task.latest_summary?.trim()) {
    lines.push(`Latest summary: ${task.latest_summary.trim()}`)
  }

  if (task.workspace_path) {
    lines.push(`Workspace: ${task.workspace_path}`)
  }

  lines.push(`For full detail run \`hermes kanban show ${task.id}\` and read the card's comments.`)

  return `${lines.join('\n')}\n\n`
}

/** Open a fresh chat through the app's New chat door with the composer
 *  prefilled (not sent) with the card's context. */
export async function startChatAboutCard(detail: KanbanTaskDetail): Promise<boolean> {
  const text = cardChatPrefill(detail)

  host.newChat()

  for (let attempt = 0; attempt < PREFILL_ATTEMPTS; attempt++) {
    if (await host.composer.setDraft('new', text)) {
      host.composer.focus('new')

      return true
    }

    await new Promise(resolve => window.setTimeout(resolve, PREFILL_RETRY_MS))
  }

  return false
}

export function ChatAboutButton({ detail, onOpened }: { detail: KanbanTaskDetail; onOpened: () => void }) {
  const k = useKanban()

  const open = () => {
    onOpened()
    void startChatAboutCard(detail).then(ok => ok || host.notify({ kind: 'error', message: k.chatAboutFailed }))
  }

  return (
    <Tip label={k.chatAboutTip}>
      <Button onClick={open} size="xs" variant="outline">
        <Codicon name="comment-discussion" size="0.75rem" />
        {k.chatAbout}
      </Button>
    </Tip>
  )
}
