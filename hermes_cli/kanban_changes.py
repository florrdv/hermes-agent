"""What a kanban card's workspace has changed so far (the desktop "Changes" tab).

The base is the commit HEAD pointed at when the task's FIRST run started (``rev-list -1
--before=<started_at> HEAD``), else ``merge-base HEAD origin/HEAD``. The summary compares the
working tree against that base, so committed, uncommitted and untracked work all show up; one
file's patch comes back on request, only for a path in that same summary (so ``file`` can never
name anything outside the work tree).

Read-only: no fetch, no index writes (``GIT_OPTIONAL_LOCKS=0`` keeps ``status`` from refreshing
the index), bounded subprocesses, and the same hardening as every automatic git call
(``noninteractive_repo_git_env`` + ``harden_git_argv``): the workspace is the agent's, so its
repo config and ``.gitattributes`` are untrusted.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_cli.kanban_timeline import _ro_connect

GIT_TIMEOUT_SECONDS = 15
MAX_PATCH_CHARS = 300_000
MAX_FILES = 1000
MAX_COMMITS = 200
_BINARY_SNIFF_BYTES = 8000
_COUNT_CHUNK_BYTES = 1 << 20
_COLLAPSED_NAMES = frozenset({"package-lock.json", "pnpm-lock.yaml", "Cargo.lock", "yarn.lock"})
# Copies are new files; type changes and unmerged paths read as modifications.
_STATUS = {"A": "A", "C": "A", "D": "D", "R": "R"}


class Unavailable(Exception):
    """The workspace cannot be diffed; the message is shown to the user as-is."""


@dataclass(frozen=True)
class _Repo:
    root: Path
    env: dict

    def git(self, *args: str, ok: tuple[int, ...] = (0,)) -> str:
        from hermes_cli._subprocess_compat import bounded_probe_run, harden_git_argv

        proc = bounded_probe_run(["git", "-C", str(self.root), *harden_git_argv(args)],
                                 timeout=GIT_TIMEOUT_SECONDS, env=self.env)
        if proc is None:
            raise Unavailable(f"git {args[0]} timed out")
        if proc.returncode not in ok:
            raise Unavailable(f"git {args[0]} failed: {(proc.stderr or '').strip()[:300]}")
        return proc.stdout


def _open_repo(workspace: Optional[str]) -> _Repo:
    from hermes_cli._subprocess_compat import noninteractive_repo_git_env

    if not workspace:
        raise Unavailable("This task has no workspace.")
    path = Path(workspace).expanduser()
    if not path.is_dir():
        raise Unavailable(f"The workspace {path} does not exist.")
    env = noninteractive_repo_git_env(path)
    if env is None:
        raise Unavailable("The workspace's git config could not be checked safely.")
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        root = _Repo(path, env).git("rev-parse", "--show-toplevel").strip()
    except Unavailable:
        raise Unavailable(f"The workspace {path} is not a git work tree.") from None
    return _Repo(Path(root), env)


def _base(repo: _Repo, started_at: Optional[float]) -> tuple[str, str]:
    """``(sha, how)``: the commit HEAD pointed at when the first run started, else the merge base."""
    if started_at:
        sha = repo.git("rev-list", "-1", f"--before=@{int(started_at)}", "HEAD").strip()
        if sha:
            return sha, "run_start"
    try:
        sha = repo.git("merge-base", "HEAD", "origin/HEAD").strip()
    except Unavailable:
        sha = ""
    if not sha:
        raise Unavailable("No base commit: nothing predates the first run and there is no origin/HEAD.")
    return sha, "merge_base"


# --- File list -------------------------------------------------------------------

def _collapsed(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in _COLLAPSED_NAMES or name.endswith(".lock")


def _file(path: str, status: str, *, old_path: Optional[str] = None, untracked: bool = False) -> dict:
    out = {"path": path, "status": status, "additions": 0, "deletions": 0, "binary": False,
           "collapsed": _collapsed(path), "untracked": untracked}
    if old_path:
        out["old_path"] = old_path
    return out


def _name_status(raw: str) -> dict[str, dict]:
    """``diff --name-status -z``: ``CODE\\0path\\0``, renames/copies ``CODE\\0old\\0new\\0``."""
    tokens, files, i = raw.split("\0"), {}, 0
    while i < len(tokens) and tokens[i]:
        code = tokens[i]
        if code[0] in "RC":
            files[tokens[i + 2]] = _file(tokens[i + 2], _STATUS[code[0]], old_path=tokens[i + 1])
            i += 3
        else:
            files[tokens[i + 1]] = _file(tokens[i + 1], _STATUS.get(code[0], "M"))
            i += 2
    return files


def _apply_numstat(raw: str, files: dict[str, dict]) -> None:
    """``diff --numstat -z``: ``add\\tdel\\tpath\\0``, renames ``add\\tdel\\t\\0old\\0new\\0``;
    binary files count ``-``."""
    tokens, i = raw.split("\0"), 0
    while i < len(tokens) and tokens[i]:
        added, deleted, path = tokens[i].split("\t", 2)
        if not path:
            path, i = tokens[i + 2], i + 3
        else:
            i += 1
        entry = files.get(path)
        if entry is None:
            continue
        if added == "-":
            entry["binary"] = True
        else:
            entry["additions"], entry["deletions"] = int(added), int(deleted)


def _count_lines(path: Path) -> tuple[int, bool]:
    """``(lines, binary)`` of an untracked file, read in bounded chunks."""
    if path.is_symlink():
        return 1, False  # git diffs a link as its one-line target
    lines, last = 0, b""
    try:
        with path.open("rb") as fh:
            chunk = fh.read(_COUNT_CHUNK_BYTES)
            if b"\0" in chunk[:_BINARY_SNIFF_BYTES]:
                return 0, True
            while chunk:
                lines, last = lines + chunk.count(b"\n"), chunk[-1:]
                chunk = fh.read(_COUNT_CHUNK_BYTES)
    except OSError:
        return 0, False
    return lines + (1 if last and last != b"\n" else 0), False


def _untracked(repo: _Repo) -> list[dict]:
    out = []
    for path in filter(None, repo.git("ls-files", "--others", "--exclude-standard", "-z").split("\0")):
        entry = _file(path, "A", untracked=True)
        lines, binary = _count_lines(repo.root / path)
        entry["additions"], entry["binary"] = lines, binary
        out.append(entry)
    return out


def _files(repo: _Repo, base: str) -> list[dict]:
    tracked = _name_status(repo.git("diff", "-M", "--name-status", "-z", base, "--"))
    _apply_numstat(repo.git("diff", "-M", "--numstat", "-z", base, "--"), tracked)
    return sorted([*tracked.values(), *_untracked(repo)], key=lambda f: f["path"])


def _commits(repo: _Repo, base: str) -> list[dict]:
    raw = repo.git("log", f"-n{MAX_COMMITS}", "--format=%H%x1f%s", f"{base}..HEAD")
    return [dict(zip(("sha", "subject"), line.split("\x1f", 1))) for line in raw.splitlines() if line]


# --- Assembly ----------------------------------------------------------------------

def _summary(repo: _Repo, started_at: Optional[float]) -> dict:
    base, how = _base(repo, started_at)
    files = _files(repo, base)
    return {
        "workspace": str(repo.root),
        "base": {"sha": base, "subject": repo.git("log", "-1", "--format=%s", base).strip(), "source": how},
        "head": {"sha": repo.git("rev-parse", "HEAD").strip()},
        "commits": _commits(repo, base),
        "dirty": bool(repo.git("status", "--porcelain", "-z").strip("\0")),
        "totals": {"files": len(files), "additions": sum(f["additions"] for f in files),
                   "deletions": sum(f["deletions"] for f in files)},
        "files_truncated": len(files) > MAX_FILES,
        "files": files[:MAX_FILES],
    }


def _patch(repo: _Repo, base: str, entry: dict) -> dict:
    if entry["untracked"]:
        # --no-index exits 1 when the files differ, which they always do here; git maps the
        # literal /dev/null on every platform (os.devnull is "nul" on Windows, which it does not).
        patch = repo.git("diff", "--no-index", "--", "/dev/null", entry["path"], ok=(0, 1))
    else:
        paths = [entry["old_path"], entry["path"]] if entry.get("old_path") else [entry["path"]]
        patch = repo.git("diff", "-M", base, "--", *paths)
    truncated = len(patch) > MAX_PATCH_CHARS
    if truncated:
        patch = patch[: patch.rfind("\n", 0, MAX_PATCH_CHARS) + 1]
    return {"path": entry["path"], "patch": patch, "truncated": truncated, "binary": entry["binary"]}


def _load_task(db: Path, task_id: str) -> tuple[dict, Optional[float]]:
    with closing(_ro_connect(db)) as conn:
        task = conn.execute("SELECT status, workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise LookupError(f"unknown task {task_id}")
        started = conn.execute("SELECT MIN(started_at) FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()[0]
    return dict(task), started


def task_changes(task_id: str, *, board: Optional[str] = None, file: Optional[str] = None,
                 kanban_db: Optional[Path] = None) -> dict:
    """The workspace's changes since the task's base, or with ``file`` that file's unified patch.
    A workspace that cannot be diffed is ``available: false`` with a ``reason``, not an error.
    Raises ``LookupError`` for an unknown task / missing board db / ``file`` not in the summary."""
    from hermes_cli import kanban_db as kb

    db = kanban_db or kb.kanban_db_path(board=board)
    if not db.is_file():
        raise LookupError(f"no kanban database at {db}")
    task, started_at = _load_task(db, task_id)
    out = {"task_id": task_id, "status": task["status"]}
    try:
        repo = _open_repo(task["workspace_path"])
        summary = _summary(repo, started_at)
        if file is None:
            return {**out, "available": True, **summary}
        entry = next((f for f in summary["files"] if f["path"] == file), None)
        if entry is None:
            raise LookupError(f"{file!r} is not among this task's changed files")
        return {**out, "available": True, **_patch(repo, summary["base"]["sha"], entry)}
    except Unavailable as exc:
        return {**out, "available": False, "reason": str(exc)}
