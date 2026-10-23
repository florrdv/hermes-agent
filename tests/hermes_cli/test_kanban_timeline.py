"""The kanban Live feed merges card events/comments, the Hermes worker's state.db session and the
Claude Code transcript into one timestamp-ordered list.

Contracts: entries from the three sources interleave by time; the returned cursor yields only
what arrived after it, including a row that lands late with an earlier timestamp, and a
transcript's partial trailing line is delivered exactly once, when it completes; note-less
heartbeats and tool results never show; long text is clipped with the full text in ``detail``;
missing state.db / transcripts contribute nothing instead of failing; an unrecorded Claude
session is inferred from the run's window.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_cli import kanban_claude_session as kcs
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_timeline as ktl
from hermes_cli.kanban_claude_transcripts import project_slug
from hermes_state import SessionDB

T0 = int(time.time()) + 1000  # after every event the kanban API stamps with "now"
SID = "local-sess-hermes"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    return home


@pytest.fixture
def projects(tmp_path) -> Path:
    path = tmp_path / ".claude" / "projects"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def workspace(tmp_path) -> Path:
    path = tmp_path / "work" / "ws"
    path.mkdir(parents=True)
    return path


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def _assistant(ts: float, *blocks: dict) -> dict:
    return {"type": "assistant", "timestamp": _iso(ts), "message": {"role": "assistant", "content": list(blocks)}}


def _write_lines(path: Path, records: list[dict], *, partial: str = "") -> None:
    with path.open("a") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in records) + partial)


def _admit(conn, tid: str, run_id: int, db: Path, ts: int, session_id: str = SID) -> None:
    with kbc.write_txn(conn):
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                     "VALUES (?, ?, 'owner_admitted', ?, ?)",
                     (tid, run_id, json.dumps({"db": str(db), "session_id": session_id}), ts))


def _restamp_latest(conn, table: str, ts: int) -> None:
    with kbc.write_txn(conn):
        conn.execute(f"UPDATE {table} SET created_at = ? WHERE id = (SELECT MAX(id) FROM {table})", (ts,))


def _card(home: Path, workspace: Path, *, record_claude: bool = True) -> tuple[str, int]:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="build it", assignee="default", workspace_kind="dir",
                             workspace_path=str(workspace))
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
        run_id = kb._current_run_id(conn, tid)
        _admit(conn, tid, run_id, home / "state.db", T0 + 1)
        kb.add_comment(conn, tid, "default", "Setup done")
        _restamp_latest(conn, "task_comments", T0 + 10)
        kbd.heartbeat_worker(conn, tid)
        _restamp_latest(conn, "task_events", T0 + 12)
        kbd.heartbeat_worker(conn, tid, note="claude: writing tests")
        _restamp_latest(conn, "task_events", T0 + 20)
        if record_claude:
            kcs.record_claude_session(conn, tid, session_id="cc-1", cwd=str(workspace))
    return tid, run_id


def _hermes_session(home: Path) -> SessionDB:
    db = SessionDB(home / "state.db")
    db.create_session(SID, "kanban")
    # SessionDB's user-turn path imports the agent loop; the prompt row only needs the real schema.
    with sqlite3.connect(home / "state.db") as raw:
        raw.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                    (SID, "# Kanban task\n\nDo the thing", T0 + 2))
    db.append_message(SID, "assistant", "Setting up the worktree.", timestamp=T0 + 5, tool_calls=[
        {"id": "c1", "type": "function",
         "function": {"name": "terminal", "arguments": json.dumps({"command": "git worktree add ../wt"})}}])
    db.append_message(SID, "tool", "Preparing worktree (new branch)", tool_call_id="c1", timestamp=T0 + 6)
    db.append_message(SID, "assistant", "Claude finished; verifying.", timestamp=T0 + 30)
    return db


def _claude_transcript(projects: Path, workspace: Path, session_id: str = "cc-1") -> Path:
    project = projects / project_slug(str(workspace))
    project.mkdir(exist_ok=True)
    path = project / f"{session_id}.jsonl"
    _write_lines(path, [
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "user", "cwd": str(workspace), "sessionId": session_id, "timestamp": _iso(T0 + 14),
         "message": {"role": "user", "content": "Implement the drawer tab"}},
        _assistant(T0 + 15, {"type": "text", "text": "Reading the drawer first."}),
        {"type": "user", "timestamp": _iso(T0 + 16),
         "message": {"content": [{"type": "tool_result", "content": "SECRET FILE BODY"}]}},
        _assistant(T0 + 21, {"type": "tool_use", "name": "Bash", "input": {"command": "npx vitest run src/x"}}),
    ], partial=json.dumps(_assistant(T0 + 25, {"type": "text", "text": "half"}))[:40])
    return path


def _live(result: dict) -> list[tuple[str, str, str]]:
    return [(e["source"], e["kind"], e["text"]) for e in result["entries"] if e["ts"] >= T0]


def test_sources_merge_in_time_order(home, projects, workspace):
    tid, _ = _card(home, workspace)
    _hermes_session(home).close()
    _claude_transcript(projects, workspace)

    result = ktl.task_timeline(tid, claude_projects_dir=projects)

    assert _live(result) == [
        ("hermes", "prompt", "task prompt: # Kanban task Do the thing"),
        ("hermes", "said", "Setting up the worktree."),
        ("hermes", "tool", "terminal git worktree add ../wt"),
        ("card", "comment", "default: Setup done"),
        ("claude", "said", "Reading the drawer first."),
        ("stage", "note", "claude: writing tests"),
        ("claude", "tool", "Bash npx vitest run src/x"),
        ("hermes", "said", "Claude finished; verifying."),
    ]
    assert [e["kind"] for e in result["entries"] if e["ts"] < T0] == ["created", "claimed"]
    assert result["now"] == {"ts": float(T0 + 20), "text": "claude: writing tests"}
    assert result["status"] == "running" and result["truncated"] is False
    assert "SECRET FILE BODY" not in json.dumps(result) and "Preparing worktree" not in json.dumps(result)


def test_since_returns_only_new_entries_even_when_they_land_late(home, projects, workspace):
    tid, _ = _card(home, workspace)
    db = _hermes_session(home)
    transcript = _claude_transcript(projects, workspace)
    first = ktl.task_timeline(tid, claude_projects_dir=projects)

    assert ktl.task_timeline(tid, since=first["cursor"], claude_projects_dir=projects)["entries"] == []

    # The partial line completes, a message is persisted with an older timestamp than the newest
    # entry already delivered, and a comment lands at the same second as an existing entry.
    with transcript.open("rb+") as fh:
        fh.truncate(transcript.stat().st_size - 40)
    _write_lines(transcript, [_assistant(T0 + 25, {"type": "text", "text": "half done"})])
    db.append_message(SID, "assistant", "late but earlier", timestamp=T0 + 3)
    db.close()
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, tid, "flor", "same second")
        _restamp_latest(conn, "task_comments", T0 + 30)

    second = ktl.task_timeline(tid, since=first["cursor"], claude_projects_dir=projects)

    assert _live(second) == [("hermes", "said", "late but earlier"), ("claude", "said", "half done"),
                             ("card", "comment", "flor: same second")]
    assert ktl.task_timeline(tid, since=second["cursor"], claude_projects_dir=projects)["entries"] == []
    ids = [e["id"] for e in first["entries"] + second["entries"]]
    assert len(ids) == len(set(ids))


def test_noteless_heartbeats_and_plumbing_events_are_skipped(home, projects, workspace):
    tid, _ = _card(home, workspace)

    kinds = [(e["source"], e["kind"]) for e in ktl.task_timeline(tid, claude_projects_dir=projects)["entries"]]

    assert ("stage", "note") in kinds
    assert not {k for _, k in kinds} & {"heartbeat", "owner_admitted", "commented"}
    assert kinds.count(("stage", "note")) == 1


def test_long_text_is_clipped_with_detail_and_tool_lines_stay_short(home, projects, workspace):
    tid, _ = _card(home, workspace, record_claude=False)
    db = SessionDB(home / "state.db")
    db.create_session(SID, "kanban")
    long_text = "word " * 400
    db.append_message(SID, "assistant", long_text, timestamp=T0 + 5, tool_calls=[
        {"id": "c1", "function": {"name": "terminal", "arguments": json.dumps({"command": "x" * 500})}}])
    db.close()

    entries = {e["kind"]: e for e in ktl.task_timeline(tid, claude_projects_dir=projects)["entries"]
               if e["source"] == "hermes"}

    assert len(entries["said"]["text"]) == ktl.TEXT_CHARS and entries["said"]["text"].endswith("…")
    assert entries["said"]["detail"] == long_text.strip()
    assert len(entries["tool"]["text"]) == ktl.TOOL_CHARS


def test_missing_state_db_and_transcript_contribute_nothing(home, projects, workspace):
    tid, _ = _card(home, workspace)  # owner_admitted names home/state.db, never created; cc-1 never written

    result = ktl.task_timeline(tid, claude_projects_dir=projects)

    assert {e["source"] for e in result["entries"]} == {"card", "stage"}
    assert not (home / "state.db").exists()


def test_unrecorded_claude_session_is_inferred_from_the_run_window(home, projects, workspace):
    tid, _ = _card(home, workspace, record_claude=False)
    _claude_transcript(projects, workspace, session_id="cc-inferred")

    texts = [e["text"] for e in ktl.task_timeline(tid, claude_projects_dir=projects)["entries"]
             if e["source"] == "claude"]

    assert texts == ["Reading the drawer first.", "Bash npx vitest run src/x"]


def test_limit_keeps_the_newest_entries(home, projects, workspace):
    tid, _ = _card(home, workspace)
    _hermes_session(home).close()

    result = ktl.task_timeline(tid, limit=2, claude_projects_dir=projects)

    assert _live(result) == [("stage", "note", "claude: writing tests"),
                             ("hermes", "said", "Claude finished; verifying.")]
    assert result["truncated"] is True


def test_unknown_task_and_bad_cursor_raise(home, projects):
    with pytest.raises(LookupError):
        ktl.task_timeline("t_missing", claude_projects_dir=projects)
    with pytest.raises(ValueError):
        ktl.Cursor.decode("not-a-cursor")
