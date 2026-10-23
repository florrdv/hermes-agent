"""Find the Claude Code transcripts a kanban run wrote, for "open session" after a worker crash.

Claude Code keeps each session at ``~/.claude/projects/<slug>/<session id>.jsonl``, where the
slug is the absolute cwd with every non-alphanumeric character replaced by ``-``. The slug is
lossy (``/a/b-c`` and ``/a/b/c`` collide), so it only narrows the directories to scan: a
transcript counts only when the ``cwd`` recorded inside it is the workspace or a path under it,
and its mtime falls inside the run's time window.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from itertools import islice
from pathlib import Path, PurePath
from typing import Optional

# Transcripts open with bookkeeping lines (``queue-operation``) that carry no cwd.
_CWD_SCAN_LINES = 50


@dataclass(frozen=True)
class Transcript:
    session_id: str
    path: str
    cwd: str
    mtime: float


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def project_slug(path: str) -> str:
    """Claude Code's project directory name for a cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _workspace_roots(workspace: str) -> list[str]:
    """The workspace as given and resolved: Claude records the cwd it saw, which may be either."""
    return list(dict.fromkeys((os.path.normpath(workspace), os.path.realpath(workspace))))


def _recorded_cwd(path: Path) -> Optional[str]:
    """The first ``cwd`` in the transcript's opening lines; ``None`` when unreadable or absent."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in islice(fh, _CWD_SCAN_LINES):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                cwd = entry.get("cwd") if isinstance(entry, dict) else None
                if isinstance(cwd, str) and os.path.isabs(cwd):
                    return cwd
    except OSError:
        return None
    return None


def _under(cwd: str, roots: list[str]) -> bool:
    target = PurePath(os.path.normpath(cwd))
    return any(target == PurePath(root) or target.is_relative_to(root) for root in roots)


def _candidate_dirs(projects_dir: Path, roots: list[str]) -> list[Path]:
    slugs = {project_slug(root) for root in roots}
    try:
        entries = list(projects_dir.iterdir())
    except OSError:
        return []
    return [entry for entry in entries
            if entry.is_dir() and any(entry.name == s or entry.name.startswith(f"{s}-") for s in slugs)]


def find_transcripts(projects_dir: Path, workspace: str, *, since: float, until: float) -> list[Transcript]:
    """Transcripts written in ``[since, until]`` by a ``claude`` whose cwd is ``workspace`` or
    under it, one per session id (newest first)."""
    roots = _workspace_roots(workspace)
    found: dict[str, Transcript] = {}
    for project in _candidate_dirs(projects_dir, roots):
        for path in project.glob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if not since <= mtime <= until:
                continue
            cwd = _recorded_cwd(path)
            if cwd is None or not _under(cwd, roots):
                continue
            prior = found.get(path.stem)
            if prior is None or mtime > prior.mtime:
                found[path.stem] = Transcript(path.stem, str(path), cwd, mtime)
    return sorted(found.values(), key=lambda t: t.mtime, reverse=True)
