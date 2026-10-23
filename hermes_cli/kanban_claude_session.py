"""Claude Code sessions on kanban cards: record where a worker ran ``claude``, then reopen it.

A worker that delegates to the Claude Code CLI (the ``claude-coder`` profile) records the
session id, the tmux session and the directory ``claude`` ran in onto its run's ``metadata``
(``hermes kanban claude-session record``, or the same keys in ``kanban_complete`` /
``kanban_request_review`` metadata). :func:`resolve_open_session` turns a task's runs into one
command: attach the live tmux session, else ``claude --resume`` the newest recorded session in
the directory it ran in, else a fresh ``claude`` in the task workspace.
``hermes kanban open-session`` and the dashboard's "Open session" button launch it in Warp
(``kanban_warp``).
"""

from __future__ import annotations

import argparse
import os
import shlex
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_runs import update_run_metadata
from hermes_cli.kanban_output import _err, _json_out

SESSION_ID_KEY = "claude_session_id"
TMUX_KEY = "tmux_session"
CWD_KEY = "claude_cwd"

MODE_ATTACH = "attach"
MODE_RESUME = "resume"
MODE_FRESH = "fresh"


def default_tmux_session(task_id: str) -> str:
    """The tmux session name the ``claude-coder`` worker convention uses for a task."""
    return f"hc-{task_id}"


# --- Recording ---------------------------------------------------------------

def _target_run_id(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]) -> int:
    """``run_id`` (must belong to ``task_id``), else the task's active run, else its newest."""
    if run_id is not None:
        row = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
        if row is None or row["task_id"] != task_id:
            raise ValueError(f"run {run_id} does not belong to task {task_id}")
        return int(run_id)
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task {task_id}")
    if task.current_run_id:
        return int(task.current_run_id)
    latest = kb.latest_run(conn, task_id)
    if latest is None:
        raise ValueError(f"task {task_id} has no runs yet; record once a worker has claimed it")
    return latest.id


def record_claude_session(
    conn: sqlite3.Connection, task_id: str, *, run_id: Optional[int] = None,
    session_id: Optional[str] = None, tmux: Optional[str] = None, cwd: Optional[str] = None,
) -> tuple[int, dict]:
    """Merge the given Claude session fields into a run's metadata; other keys survive.
    Returns ``(run_id, stored metadata)``."""
    fields = {
        key: value.strip()
        for key, value in ((SESSION_ID_KEY, session_id), (TMUX_KEY, tmux), (CWD_KEY, cwd))
        if value and value.strip()
    }
    if not fields:
        raise ValueError("nothing to record: pass --session-id, --tmux and/or --cwd")
    with kbc.write_txn(conn):
        rid = _target_run_id(conn, task_id, run_id)
        return rid, update_run_metadata(conn, rid, fields)


# --- Resolution ----------------------------------------------------------------

@dataclass(frozen=True)
class OpenSessionPlan:
    """What "open session" will run: ``argv`` in ``cwd`` (``None`` = the user's home)."""

    task_id: str
    mode: str
    argv: tuple[str, ...]
    cwd: Optional[str]
    reason: str

    @property
    def command(self) -> str:
        return shlex.join(self.argv)

    @property
    def shell_line(self) -> str:
        """One line a user can paste into any shell."""
        return f"cd {shlex.quote(self.cwd)} && {self.command}" if self.cwd else self.command

    def to_dict(self) -> dict:
        return {**asdict(self), "argv": list(self.argv), "command": self.command, "shell_line": self.shell_line}


def _recorded(runs: Iterable[kb.Run], key: str) -> list[tuple[kb.Run, str]]:
    """``(run, value)`` for every run whose metadata carries ``key``, newest run first."""
    newest_first = sorted(runs, key=lambda r: (r.started_at, r.id), reverse=True)
    out = []
    for run in newest_first:
        value = (run.metadata or {}).get(key) if isinstance(run.metadata, dict) else None
        if isinstance(value, str) and value.strip():
            out.append((run, value.strip()))
    return out


def _existing_dir(path: object, is_dir: Callable[[str], bool]) -> Optional[str]:
    """``path`` when it is an absolute directory that exists (metadata values are untyped)."""
    return path if isinstance(path, str) and os.path.isabs(path) and is_dir(path) else None


def resolve_open_session(
    task: kb.Task, runs: Iterable[kb.Run], *, tmux_alive: Callable[[str], bool],
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> OpenSessionPlan:
    """attach (live tmux) > resume (newest recorded Claude session) > fresh ``claude``.

    tmux candidates are the newest recorded ``tmux_session`` then the ``hc-<task_id>``
    default. ``=name`` makes tmux match the session name exactly (``hc-t_1`` must not
    attach ``hc-t_12``)."""
    runs = list(runs)
    workspace = _existing_dir(task.workspace_path, is_dir)
    names = [name for _run, name in _recorded(runs, TMUX_KEY)[:1]] + [default_tmux_session(task.id)]
    for name in dict.fromkeys(names):
        if tmux_alive(name):
            return OpenSessionPlan(task.id, MODE_ATTACH, ("tmux", "attach", "-t", f"={name}"), workspace,
                                   f"tmux session {name!r} is alive")
    sessions = _recorded(runs, SESSION_ID_KEY)
    if sessions:
        run, session_id = sessions[0]
        recorded_cwd = (run.metadata or {}).get(CWD_KEY)
        cwd = _existing_dir(recorded_cwd, is_dir)
        if cwd:
            where = f"recorded cwd {cwd}"
        elif recorded_cwd:
            cwd, where = workspace, f"recorded cwd {recorded_cwd} is gone; task workspace"
        else:
            cwd, where = workspace, "task workspace (no cwd recorded)"
        return OpenSessionPlan(task.id, MODE_RESUME, ("claude", "--resume", session_id), cwd,
                               f"no live tmux session; resuming Claude session from run {run.id} in {where}")
    return OpenSessionPlan(task.id, MODE_FRESH, ("claude",), workspace,
                           "no live tmux session and no recorded Claude session"
                           + ("" if workspace else "; task workspace does not exist, starting in home"))


def tmux_session_alive(name: str) -> bool:
    """``tmux has-session`` for an exact session name; no tmux installed = not alive."""
    from hermes_platform.resolver import locate_command
    from hermes_platform.resolver.known_dirs import homebrew_dirs

    tmux = locate_command("tmux", known_dirs=homebrew_dirs()).command
    if not tmux:
        return False
    try:
        proc = subprocess.run([*tmux, "has-session", "-t", f"={name}"], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def plan_for_task(conn: sqlite3.Connection, task_id: str) -> OpenSessionPlan:
    task = kb.get_task(conn, task_id)
    if task is None:
        raise LookupError(f"unknown task {task_id}")
    return resolve_open_session(task, kb.list_runs(conn, task_id), tmux_alive=tmux_session_alive)


def open_session(conn: sqlite3.Connection, task_id: str, *, launch: bool = True) -> dict:
    """Resolve the plan and (``launch``) open it in Warp. The result always carries the plan;
    ``launched`` is False with ``error`` set when Warp is unavailable, so callers can show the
    command for the user to run by hand."""
    from hermes_cli import kanban_warp

    plan = plan_for_task(conn, task_id)
    result = {**plan.to_dict(), "launched": False, "error": None, "launch_config": None}
    if not launch:
        return result
    try:
        result["launch_config"] = str(kanban_warp.launch_in_warp(plan))
        result["launched"] = True
    except kanban_warp.WarpUnavailable as exc:
        result["error"] = str(exc)
    return result


# --- CLI handlers (wired by the ``hermes_cli.kanban`` facade) ------------------------

def _env_run_id(task_id: str) -> Optional[int]:
    """The worker's own run id, only when the env's task is the one being recorded."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    return int(raw) if raw.isdigit() else None


def _cmd_claude_session(args: argparse.Namespace) -> int:
    """``hermes kanban claude-session record``; task/run default from the worker env."""
    if getattr(args, "claude_session_action", None) != "record":
        return _err("usage: hermes kanban claude-session record [--task T] [--run R] "
                    "[--session-id ID] [--tmux NAME] [--cwd DIR]", 2)
    task_id = args.task or os.environ.get("HERMES_KANBAN_TASK")
    if not task_id:
        return _err("kanban: --task is required outside a kanban worker (HERMES_KANBAN_TASK unset)", 2)
    run_id = args.run if args.run is not None else _env_run_id(task_id)
    cwd = str(Path(args.cwd).expanduser().resolve()) if args.cwd else None
    with kbc.connect_closing() as conn:
        rid, metadata = record_claude_session(conn, task_id, run_id=run_id, session_id=args.session_id,
                                              tmux=args.tmux, cwd=cwd)
    if _json_out(args, {"task_id": task_id, "run_id": rid, "metadata": metadata}):
        return 0
    recorded = ", ".join(f"{k}={metadata[k]}" for k in (SESSION_ID_KEY, TMUX_KEY, CWD_KEY) if k in metadata)
    print(f"Recorded on {task_id} run {rid}: {recorded}")
    return 0


def _print_plan(result: dict) -> None:
    print(f"mode:    {result['mode']}")
    print(f"cwd:     {result['cwd'] or '~ (home)'}")
    print(f"command: {result['command']}")
    print(f"reason:  {result['reason']}")


def _cmd_open_session(args: argparse.Namespace) -> int:
    """``hermes kanban open-session <task_id> [--print]``."""
    with kbc.connect_closing() as conn:
        try:
            result = open_session(conn, args.task_id, launch=not args.print_only)
        except LookupError as exc:
            return _err(f"kanban: {exc}")
    if _json_out(args, result):
        return 1 if result["error"] else 0
    _print_plan(result)
    if result["launched"]:
        print(f"Opened in Warp ({result['launch_config']})")
    elif result["error"]:
        print(f"\nCould not open Warp: {result['error']}\nRun it yourself:\n  {result['shell_line']}")
        return 1
    return 0
