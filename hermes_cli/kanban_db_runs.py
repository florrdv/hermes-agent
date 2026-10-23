"""Run-row writes for the Kanban DB: closing the active run and merging into a
run's ``metadata`` column.

A run's metadata can be written while the worker is still running (``hermes
kanban claude-session record``), so closing the run merges the handoff
metadata over what is already stored instead of replacing it.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional


def merged_run_metadata(conn: sqlite3.Connection, run_id: int, patch: Optional[dict]) -> Optional[dict]:
    """``patch`` shallow-merged over run ``run_id``'s stored metadata (patch keys win).
    Unparseable or non-object stored metadata is replaced rather than merged."""
    row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
    stored = _kb._json_or(_kb._lossy_text(row["metadata"])) if row else None
    if not isinstance(stored, dict) or not stored:
        return patch
    return {**stored, **(patch or {})}


def _end_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None, status: Optional[str] = None,
) -> Optional[int]:
    """Close the active run (``status`` defaults to ``outcome``) and clear
    ``current_run_id``; None when no run was active (never-claimed task).

    ``metadata`` merges over what the worker recorded during the run (see
    :func:`merged_run_metadata`), so a completion handoff never erases it.

    ``worker_pid`` / ``worker_started_at`` / ``claim_lock`` stay on the closed
    row: they are the only evidence left of the OS process once the task row
    is wiped, and :func:`kanban_db_dispatch.reap_terminal_workers` needs them
    to end a worker that survived its own terminal transition."""
    now = int(time.time())
    run_id = _kb._current_run_id(conn, task_id)
    if run_id is None:
        return None
    metadata = merged_run_metadata(conn, run_id, metadata)
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_expires = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (status or outcome, outcome, summary, error, _kb._json_or_null(metadata), now, run_id),
    )
    conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
    return run_id


def update_run_metadata(conn: sqlite3.Connection, run_id: int, patch: dict) -> dict:
    """Merge ``patch`` into run ``run_id``'s metadata (open or closed run); returns the
    stored result. The caller owns the transaction."""
    merged = merged_run_metadata(conn, run_id, patch) or {}
    conn.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = ?",
        (json.dumps(merged, ensure_ascii=False), int(run_id)),
    )
    return merged


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
