"""Kanban dashboard plugin: GET /tasks/{id}/changes diffs the task's workspace against the commit
HEAD pointed at when the first run started — later commits, uncommitted edits and untracked files
all count — and ``?file=`` returns one listed file's patch. A non-git workspace is
``available: false``; a path outside the summary is 404."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

RUN_START = 1_790_000_000


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_changes_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _git(repo: Path, *args: str, when: int | None = None) -> str:
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{when} +0000"
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True, env=env).stdout.strip()


def _task_with_run(workspace: Path) -> str:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default", workspace_kind="dir", workspace_path=str(workspace))
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
        conn.execute("UPDATE task_runs SET started_at = ? WHERE task_id = ?", (RUN_START, tid))
        conn.commit()
    return tid


@pytest.fixture
def repo(tmp_path) -> Path:
    """main.py committed before the run; a commit during it (feature.py, main.py edit, a
    lockfile); then an uncommitted edit to main.py and an untracked notes.txt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "main.py").write_text("a\nb\nc\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "before the run", when=RUN_START - 100)
    (repo / "feature.py").write_text("x = 1\n")
    (repo / "main.py").write_text("a\nB\nc\n")
    (repo / "yarn.lock").write_text("lock\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "during the run", when=RUN_START + 100)
    (repo / "main.py").write_text("a\nB\nc\nd\n")
    (repo / "notes.txt").write_text("one\ntwo\n")
    return repo


def _changes(client: TestClient, tid: str, **params) -> dict:
    resp = client.get(f"/api/plugins/kanban/tasks/{tid}/changes", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_summary_counts_commits_uncommitted_and_untracked_work_since_the_first_run(client, repo):
    tid = _task_with_run(repo)

    body = _changes(client, tid)

    assert body["available"] is True
    assert body["base"]["subject"] == "before the run" and body["base"]["source"] == "run_start"
    assert body["head"]["sha"] == _git(repo, "rev-parse", "HEAD")
    assert [c["subject"] for c in body["commits"]] == ["during the run"]
    assert body["dirty"] is True
    files = {f["path"]: f for f in body["files"]}
    assert {p: (f["status"], f["additions"], f["deletions"], f["untracked"]) for p, f in files.items()} == {
        "feature.py": ("A", 1, 0, False),
        "main.py": ("M", 2, 1, False),
        "notes.txt": ("A", 2, 0, True),
        "yarn.lock": ("A", 1, 0, False),
    }
    assert [p for p, f in files.items() if f["collapsed"]] == ["yarn.lock"]
    assert body["totals"] == {"files": 4, "additions": 6, "deletions": 1}


def test_file_patch_for_tracked_and_untracked_files(client, repo):
    tid = _task_with_run(repo)

    tracked = _changes(client, tid, file="main.py")
    untracked = _changes(client, tid, file="notes.txt")

    assert tracked["truncated"] is False
    assert "-b\n+B\n" in tracked["patch"] and "+d\n" in tracked["patch"] and "@@" in tracked["patch"]
    assert "+one\n+two\n" in untracked["patch"] and "new file mode" in untracked["patch"]


def test_file_outside_the_summary_is_404(client, repo):
    tid = _task_with_run(repo)

    for path in ("../outside.txt", "/etc/passwd", "README.md"):
        assert client.get(f"/api/plugins/kanban/tasks/{tid}/changes", params={"file": path}).status_code == 404


def test_rename_reports_old_path(client, tmp_path):
    repo = tmp_path / "renamed"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "old.txt").write_text("".join(f"line {i}\n" for i in range(20)))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base", when=RUN_START - 100)
    _git(repo, "mv", "old.txt", "new.txt")
    tid = _task_with_run(repo)

    files = _changes(client, tid)["files"]

    assert [(f["path"], f["status"], f.get("old_path")) for f in files] == [("new.txt", "R", "old.txt")]
    assert "rename from old.txt" in _changes(client, tid, file="new.txt")["patch"]


def test_non_git_workspace_is_unavailable(client, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    tid = _task_with_run(plain)

    body = _changes(client, tid)

    assert body["available"] is False and "not a git work tree" in body["reason"]


def test_missing_workspace_is_unavailable_and_unknown_task_404(client, tmp_path):
    tid = _task_with_run(tmp_path / "gone")

    assert "does not exist" in _changes(client, tid)["reason"]
    assert client.get("/api/plugins/kanban/tasks/t_missing/changes").status_code == 404
