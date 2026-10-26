"""Which session sources ``projects.tree`` places in the sidebar.

Kanban workers, cron runs and one-shot runs are not user conversations and stay out of the tree,
whether a worker ran in a task worktree (``<repo>/.worktrees/t_<hex>``) or a ``dir:`` workspace."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tui_gateway import git_probe, server


def _call(method, params=None):
    resp = server._methods[method](1, params or {})
    assert "error" not in resp, resp.get("error")
    return resp["result"]


@pytest.fixture(autouse=True)
def _fake_git(monkeypatch):
    """Answer the two probes ``git_probe.resolve`` makes from the on-disk ``.git`` layout: a
    ``.git`` dir is a main checkout, a ``.git`` file (``gitdir: <repo>/.git/worktrees/<n>``) a
    linked worktree, as ``git worktree add`` writes them."""
    def _toplevel(cwd: str) -> str:
        d = cwd
        while d and d != os.path.dirname(d):
            if os.path.exists(os.path.join(d, ".git")):
                return d
            d = os.path.dirname(d)
        return ""

    def _run_git(cwd, *args):
        top = _toplevel(str(cwd))
        if not top or args[-1] != "--git-common-dir":
            return top
        dotgit = os.path.join(top, ".git")
        if os.path.isdir(dotgit):
            return dotgit
        gitdir = Path(dotgit).read_text().split("gitdir:", 1)[1].strip()
        return os.path.dirname(os.path.dirname(gitdir))

    monkeypatch.setattr(git_probe, "run_git", _run_git)


def _linked_worktree(repo: Path, path: Path) -> None:
    admin = repo / ".git" / "worktrees" / path.name
    admin.mkdir(parents=True)
    path.mkdir(parents=True)
    (path / ".git").write_text(f"gitdir: {admin}\n")


@contextlib.contextmanager
def _serving(home: Path):
    from hermes_state import SessionDB

    token = set_hermes_home_override(home)
    prev = server._db, server._db_error, server._hermes_home
    server._hermes_home = home
    server._db = SessionDB(db_path=home / "state.db")
    server._db_error = None
    git_probe.invalidate()
    try:
        yield server._db
    finally:
        server._db.close()
        server._db, server._db_error, server._hermes_home = prev
        reset_hermes_home_override(token)
        git_probe.invalidate()


def _seed(db, session_id: str, source: str, cwd: Path) -> None:
    db.create_session(session_id, source, cwd=str(cwd))
    db.append_message(session_id, "user", f"hello from {session_id}")


def test_kanban_cron_and_oneshot_sessions_stay_out_of_the_tree(tmp_path):
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    repo = root / "repos" / "app"
    (repo / ".git").mkdir(parents=True)
    task_wt = repo / ".worktrees" / "t_0a1b2c3d"
    _linked_worktree(repo, task_wt)
    dir_workspace = root / "repos" / "scratch-notes"
    dir_workspace.mkdir()

    with _serving(home) as db:
        _seed(db, "chat", "cli", repo)
        _seed(db, "worker-wt", "kanban", task_wt)
        _seed(db, "worker-dir", "kanban", dir_workspace)
        _seed(db, "cron-run", "cron", repo)
        _seed(db, "oneshot-run", "oneshot", repo)
        tree = _call("projects.tree")
        drilled = _call("projects.project_sessions", {"project_id": str(repo)})["project"]

    assert set(tree["scoped_session_ids"]) == {"chat"}
    assert str(dir_workspace) not in {p["id"] for p in tree["projects"]}

    listed = {s["id"] for r in drilled["repos"] for g in r["groups"] for s in g["sessions"]}
    assert listed == {"chat"}
