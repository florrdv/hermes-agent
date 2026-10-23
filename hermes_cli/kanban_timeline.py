"""One time-ordered feed of what a kanban card's agent is doing (the desktop "Live" tab).

Three read-only sources are merged by timestamp:

- **card** (``kanban.db``): comments and task events; heartbeats carrying a ``note`` become
  ``stage`` entries, note-less heartbeats and bookkeeping events are dropped.
- **hermes**: the worker's own session in ``state.db`` (session ids from each run's
  ``owner_admitted`` event, whose payload may name the profile's ``state.db``); assistant text and
  one-line tool calls, never tool results.
- **claude**: Claude Code transcripts the run recorded (``claude_session_id`` / ``claude_cwd``
  run metadata) or, when nothing was recorded, the ones inferred for the run's window with the
  same rules "open session" uses (``kanban_claude_transcripts``).

Polling is incremental: the response's ``cursor`` holds a high-water mark per source (event id,
comment id, message id per session, byte offset per transcript), so a row that lands late with an
earlier timestamp is still delivered once, and equal timestamps can neither drop nor repeat an
entry. Clients merge new entries by ``(ts, id)``. Every database is opened ``mode=ro``; a missing
file contributes nothing, and a transcript's partial trailing line is left for the next poll.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from hermes_cli import kanban_claude_transcripts as kct

DEFAULT_LIMIT = 300
MAX_LIMIT = 2000
TEXT_CHARS = 300
TOOL_CHARS = 160
DETAIL_CHARS = 4000
# Same slack "open session" uses: run timestamps are whole seconds, transcript mtimes are not.
TRANSCRIPT_WINDOW_SLACK_SECONDS = 60

SOURCE_HERMES, SOURCE_CARD, SOURCE_STAGE, SOURCE_CLAUDE = "hermes", "card", "stage", "claude"
# Tiebreak for equal timestamps: what the card says happened, then who did it.
_SOURCE_RANK = {SOURCE_CARD: 0, SOURCE_STAGE: 1, SOURCE_HERMES: 2, SOURCE_CLAUDE: 3}

# Events that only restate another row (``commented``) or are plumbing nobody reads.
_SKIPPED_EVENTS = frozenset({"worker_bound", "owner_admitted", "commented", "archive_worker_termination"})
_PAYLOAD_KEYS = ("status", "outcome", "summary", "reason", "error", "exit_code")
_HERMES_TOOL_KEYS = ("command", "path", "file_path", "query", "url", "pattern", "description")
_CLAUDE_TOOL_KEYS = ("command", "file_path", "pattern", "description", "path", "url", "query")


# --- Text helpers ----------------------------------------------------------------

def clip(text: object, limit: int = TEXT_CHARS) -> str:
    """Whitespace-collapsed ``text``, cut to ``limit`` characters with an ellipsis."""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _detail(text: object, shown: str) -> Optional[str]:
    """The fuller text behind a clipped line, only when clipping hid something."""
    full = str(text or "").strip()
    if len(full) <= len(shown):
        return None
    return full if len(full) <= DETAIL_CHARS else full[: DETAIL_CHARS - 1] + "…"


def _loads(raw: object) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _first_arg(args: object, keys: tuple[str, ...]) -> object:
    """The most telling argument of a tool call: the first of ``keys`` present, else any."""
    if not isinstance(args, dict):
        return args or ""
    for key in keys:
        if args.get(key):
            return args[key]
    return next(iter(args.values()), "")


def _iso_to_epoch(raw: object) -> Optional[float]:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


# --- Entries and cursor ----------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    ts: float
    source: str
    kind: str
    text: str
    id: str
    seq: tuple = ()
    detail: Optional[str] = None

    @property
    def order(self) -> tuple:
        return (self.ts, _SOURCE_RANK.get(self.source, 9), self.id.split(":", 1)[0], self.seq)

    def to_dict(self) -> dict:
        out = {"id": self.id, "ts": self.ts, "source": self.source, "kind": self.kind, "text": self.text}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class Cursor:
    """Per-source high-water marks; opaque to clients (``encode`` / ``decode``)."""

    events: int = 0
    comments: int = 0
    messages: dict[str, int] = field(default_factory=dict)
    transcripts: dict[str, int] = field(default_factory=dict)

    def encode(self) -> str:
        raw = json.dumps({"e": self.events, "c": self.comments, "m": self.messages, "t": self.transcripts},
                         separators=(",", ":"), sort_keys=True)
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, raw: Optional[str]) -> Cursor:
        if not raw:
            return cls()
        try:
            data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            return cls(int(data["e"]), int(data["c"]),
                       {str(k): int(v) for k, v in data["m"].items()},
                       {str(k): int(v) for k, v in data["t"].items()})
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError(f"invalid timeline cursor: {raw!r}") from exc


# --- Read-only sqlite ------------------------------------------------------------

def _ro_connect(path: Path) -> sqlite3.Connection:
    """``mode=ro`` connection; never creates the file, never takes a write lock."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    return conn


# --- Card: events and comments ---------------------------------------------------

def _event_bits(payload: dict) -> str:
    bits = []
    for key in _PAYLOAD_KEYS:
        value = payload.get(key)
        if value in (None, ""):
            continue
        bits.append(f"{key}={value}" if key in ("status", "exit_code") else str(value))
    return " ".join(bits)


def _text_created(p: dict) -> str:
    return f"created (assignee {p['assignee']})" if p.get("assignee") else "created"


def _text_spawned(p: dict) -> str:
    return f"worker spawned (pid {p['pid']})" if p.get("pid") else "worker spawned"


def _text_worker_result(p: dict) -> str:
    return f"worker exited (exit {p.get('exit_code')})"


def _text_crashed(p: dict) -> str:
    return "crashed: " + str(p.get("error") or p.get("worker_output") or "")


def _labelled(label: str, *keys: str) -> Callable[[dict], str]:
    def _text(p: dict) -> str:
        value = next((p[k] for k in keys if p.get(k)), "")
        return f"{label}: {value}" if value else label
    return _text


_EVENT_TEXT: dict[str, Callable[[dict], str]] = {
    "created": _text_created,
    "claimed": lambda p: "claimed by a worker",
    "spawned": _text_spawned,
    "worker_result": _text_worker_result,
    "crashed": _text_crashed,
    "completed": _labelled("completed", "summary", "result"),
    "blocked": _labelled("blocked", "reason"),
    "review_requested": _labelled("review requested", "summary"),
    "changes_requested": _labelled("changes requested", "reason"),
    "assigned": _labelled("assigned", "assignee"),
    "reclaimed": _labelled("reclaimed", "reason"),
    "gave_up": _labelled("gave up", "error"),
    "spawn_failed": _labelled("spawn failed", "error"),
}


def _event_entry(row: sqlite3.Row) -> Optional[Entry]:
    kind, payload = row["kind"], _as_dict(_loads(row["payload"]))
    eid = f"e:{row['id']}"
    if kind == "heartbeat":
        note = payload.get("note")
        if not note:
            return None
        text = clip(note)
        return Entry(float(row["created_at"]), SOURCE_STAGE, "note", text, eid, (row["id"],), _detail(note, text))
    if kind in _SKIPPED_EVENTS:
        return None
    formatter = _EVENT_TEXT.get(kind)
    full = formatter(payload) if formatter else " ".join(filter(None, (kind, _event_bits(payload))))
    text = clip(full)
    return Entry(float(row["created_at"]), SOURCE_CARD, kind, text, eid, (row["id"],), _detail(full, text))


def _card_entries(conn: sqlite3.Connection, task_id: str, cursor: Cursor) -> list[Entry]:
    out: list[Entry] = []
    for row in conn.execute("SELECT id, kind, payload, created_at FROM task_events "
                            "WHERE task_id = ? AND id > ? ORDER BY id", (task_id, cursor.events)):
        cursor.events = max(cursor.events, row["id"])
        entry = _event_entry(row)
        if entry:
            out.append(entry)
    for row in conn.execute("SELECT id, author, body, created_at FROM task_comments "
                            "WHERE task_id = ? AND id > ? ORDER BY id", (task_id, cursor.comments)):
        cursor.comments = max(cursor.comments, row["id"])
        full = f"{row['author']}: {row['body']}"
        text = clip(full)
        out.append(Entry(float(row["created_at"]), SOURCE_CARD, "comment", text, f"c:{row['id']}",
                         (row["id"],), _detail(full, text)))
    return out


def _latest_note(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    row = conn.execute("SELECT json_extract(payload, '$.note') AS note, created_at FROM task_events "
                       "WHERE task_id = ? AND kind = 'heartbeat' AND json_valid(payload) "
                       "AND json_extract(payload, '$.note') IS NOT NULL ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    return {"ts": float(row["created_at"]), "text": clip(row["note"])} if row and row["note"] else None


# --- Hermes worker session (state.db) ---------------------------------------------

def _worker_sessions(conn: sqlite3.Connection, task_id: str, runs: list[dict]) -> dict[str, Path]:
    """``{session id: state.db path}`` for every run: the ``owner_admitted`` binding (its ``db``
    names the worker profile's state.db), else the run's recorded ``worker_session_id``."""
    from hermes_constants import get_hermes_home

    default_db = get_hermes_home() / "state.db"
    sessions: dict[str, Path] = {}
    for row in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'owner_admitted' "
                            "ORDER BY id", (task_id,)):
        payload = _as_dict(_loads(row["payload"]))
        sid, db = payload.get("session_id"), payload.get("db")
        if isinstance(sid, str) and sid:
            sessions.setdefault(sid, Path(db) if isinstance(db, str) and os.path.isabs(db) else default_db)
    for run in runs:
        sid = run["metadata"].get("worker_session_id")
        if isinstance(sid, str) and sid:
            sessions.setdefault(sid, default_db)
    return sessions


def _tool_call_line(call: object) -> Optional[str]:
    fn = _as_dict(_as_dict(call).get("function", call))
    if not fn.get("name"):
        return None
    args = fn.get("arguments")
    args = _loads(args) if isinstance(args, str) else args
    return clip(f"{fn['name']} {_first_arg(args, _HERMES_TOOL_KEYS)}", TOOL_CHARS)


def _message_entries(sid: str, row: sqlite3.Row) -> Iterator[Entry]:
    ts, mid, short = float(row["timestamp"]), row["id"], sid[-8:]
    if row["role"] == "user" and row["first"]:
        yield Entry(ts, SOURCE_HERMES, "prompt", "task prompt: " + clip(row["content"], 100),
                    f"m:{short}:{mid}", (mid, 0))
        return
    if row["role"] != "assistant":
        return
    content = row["content"] or ""
    if content.strip():
        text = clip(content)
        yield Entry(ts, SOURCE_HERMES, "said", text, f"m:{short}:{mid}", (mid, 0), _detail(content, text))
    calls = _loads(row["tool_calls"])
    for index, call in enumerate(calls if isinstance(calls, list) else []):
        line = _tool_call_line(call)
        if line:
            yield Entry(ts, SOURCE_HERMES, "tool", line, f"m:{short}:{mid}:{index}", (mid, index + 1))


def _session_entries(db: Path, sid: str, cursor: Cursor) -> list[Entry]:
    if not db.is_file():
        return []
    after = cursor.messages.get(sid, 0)
    out: list[Entry] = []
    with closing(_ro_connect(db)) as conn:
        first_id = conn.execute("SELECT MIN(id) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0]
        rows = conn.execute("SELECT id, role, content, tool_calls, timestamp, id = ? AS first FROM messages "
                            "WHERE session_id = ? AND id > ? ORDER BY id", (first_id, sid, after))
        for row in rows:
            cursor.messages[sid] = max(cursor.messages.get(sid, 0), row["id"])
            out.extend(_message_entries(sid, row))
    return out


def _hermes_entries(sessions: dict[str, Path], cursor: Cursor) -> list[Entry]:
    out: list[Entry] = []
    for sid, db in sessions.items():
        try:
            out.extend(_session_entries(db, sid, cursor))
        except sqlite3.Error:
            continue  # a state.db mid-migration or locked out; the next poll retries from the same mark
    return out


# --- Claude Code transcripts -------------------------------------------------------

def _recorded_transcript(projects_dir: Path, meta: dict) -> Optional[Path]:
    sid = meta.get("claude_session_id")
    if not isinstance(sid, str) or not sid.strip() or "/" in sid:
        return None
    sid, cwd = sid.strip(), meta.get("claude_cwd")
    if isinstance(cwd, str) and cwd:
        path = projects_dir / kct.project_slug(cwd) / f"{sid}.jsonl"
        if path.is_file():
            return path
    return next(iter(sorted(projects_dir.glob(f"*/{sid}.jsonl"))), None)


def _inferred_transcripts(projects_dir: Path, workspace: Optional[str], run: dict) -> list[Path]:
    if not workspace or not run["started_at"]:
        return []
    until = run["ended_at"] if run["ended_at"] is not None else time.time()
    found = kct.find_transcripts(projects_dir, workspace, since=run["started_at"] - TRANSCRIPT_WINDOW_SLACK_SECONDS,
                                 until=until + TRANSCRIPT_WINDOW_SLACK_SECONDS)
    return [Path(t.path) for t in found]


def claude_transcripts(projects_dir: Path, workspace: Optional[str], runs: list[dict]) -> dict[str, Path]:
    """``{session id: transcript}`` across runs: each run's recorded session, else every
    transcript inferred for its window (a run that crashed before recording, or ran several)."""
    out: dict[str, Path] = {}
    for run in runs:
        recorded = _recorded_transcript(projects_dir, run["metadata"])
        for path in [recorded] if recorded else _inferred_transcripts(projects_dir, workspace, run):
            out.setdefault(path.stem, path)
    return out


def _claude_block(block: dict, ts: float, ident: str, seq: tuple) -> Optional[Entry]:
    if block.get("type") == "text" and str(block.get("text") or "").strip():
        text = clip(block["text"])
        return Entry(ts, SOURCE_CLAUDE, "said", text, ident, seq, _detail(block["text"], text))
    if block.get("type") == "tool_use":
        arg = _first_arg(block.get("input"), _CLAUDE_TOOL_KEYS)
        return Entry(ts, SOURCE_CLAUDE, "tool", clip(f"{block.get('name') or '?'} {arg}", TOOL_CHARS), ident, seq)
    return None


def _claude_line(line: bytes, short: str, offset: int) -> Iterator[Entry]:
    record = _loads(line)
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return
    ts = _iso_to_epoch(record.get("timestamp"))
    content = _as_dict(record.get("message")).get("content")
    if ts is None or not isinstance(content, list):
        return
    for index, block in enumerate(content):
        entry = _claude_block(_as_dict(block), ts, f"t:{short}:{offset}:{index}", (offset, index))
        if entry:
            yield entry


def _transcript_entries(sid: str, path: Path, cursor: Cursor) -> list[Entry]:
    start = cursor.transcripts.get(sid, 0)
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    complete = data.rfind(b"\n") + 1  # a line still being written waits for the next poll
    cursor.transcripts[sid] = start + complete
    out: list[Entry] = []
    offset = start
    for line in data[:complete].splitlines(keepends=True):
        out.extend(_claude_line(line, sid[:8], offset))
        offset += len(line)
    return out


# --- Assembly ----------------------------------------------------------------------

def _load_task(conn: sqlite3.Connection, task_id: str) -> tuple[dict, list[dict]]:
    task = conn.execute("SELECT id, status, workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise LookupError(f"unknown task {task_id}")
    runs = [{"id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"],
             "metadata": _as_dict(_loads(r["metadata"]))}
            for r in conn.execute("SELECT id, started_at, ended_at, metadata FROM task_runs "
                                  "WHERE task_id = ? ORDER BY id", (task_id,))]
    return dict(task), runs


def task_timeline(task_id: str, *, board: Optional[str] = None, since: Optional[str] = None,
                  limit: int = DEFAULT_LIMIT, kanban_db: Optional[Path] = None,
                  claude_projects_dir: Optional[Path] = None) -> dict:
    """The card's merged feed. Without ``since``: the newest ``limit`` entries. With it: every
    entry past that cursor (the newest ``limit`` if more arrived; ``truncated`` says so).
    Raises ``LookupError`` for an unknown task / missing board db, ``ValueError`` for a bad cursor."""
    from hermes_cli import kanban_db as kb

    cursor = Cursor.decode(since)
    db = kanban_db or kb.kanban_db_path(board=board)
    if not db.is_file():
        raise LookupError(f"no kanban database at {db}")
    projects_dir = claude_projects_dir or kct.default_projects_dir()
    with closing(_ro_connect(db)) as conn:
        task, runs = _load_task(conn, task_id)
        entries = _card_entries(conn, task_id, cursor)
        sessions = _worker_sessions(conn, task_id, runs)
        now = _latest_note(conn, task_id)
    entries += _hermes_entries(sessions, cursor)
    for sid, path in claude_transcripts(projects_dir, task["workspace_path"], runs).items():
        entries += _transcript_entries(sid, path, cursor)
    entries.sort(key=lambda e: e.order)
    limit = max(1, min(int(limit), MAX_LIMIT))
    return {"task_id": task_id, "status": task["status"], "cursor": cursor.encode(), "now": now,
            "truncated": len(entries) > limit, "entries": [e.to_dict() for e in entries[-limit:]]}
