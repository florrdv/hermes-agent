"""Kanban dashboard plugin: POST /tasks/{id}/open-session resolves the card's Claude Code
session and hands it to the Warp launcher (mocked here). Warp being unavailable is a 200
whose body carries the command for the user to run; an unknown task is a 404."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_claude_session as kcs
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_warp


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_open_session_test", plugin_file)
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
    monkeypatch.setattr(kcs, "tmux_session_alive", lambda name: False)
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _task_with_session(tmp_path) -> str:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="claude-coder", workspace_kind="dir",
                             workspace_path=str(tmp_path))
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
        kcs.record_claude_session(conn, tid, session_id="sess-7", cwd=str(tmp_path))
    return tid


def test_open_session_launches_resolved_plan(client, tmp_path, monkeypatch):
    tid = _task_with_session(tmp_path)
    launched = []
    monkeypatch.setattr(kanban_warp, "launch_in_warp",
                        lambda plan: launched.append(plan) or tmp_path / "hermes-x.yaml")

    r = client.post(f"/api/plugins/kanban/tasks/{tid}/open-session")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["launched"] is True and body["error"] is None
    assert (body["mode"], body["command"], body["cwd"]) == ("resume", "claude --resume sess-7", str(tmp_path))
    assert [p.argv for p in launched] == [("claude", "--resume", "sess-7")]


def test_open_session_without_warp_returns_command_to_run(client, tmp_path, monkeypatch):
    tid = _task_with_session(tmp_path)

    def _unavailable(plan):
        raise kanban_warp.WarpUnavailable("opening in Warp is supported on macOS only")

    monkeypatch.setattr(kanban_warp, "launch_in_warp", _unavailable)

    body = client.post(f"/api/plugins/kanban/tasks/{tid}/open-session").json()

    assert body["launched"] is False
    assert "macOS only" in body["error"]
    assert body["shell_line"].endswith("claude --resume sess-7")


def test_open_session_unknown_task_is_404(client, monkeypatch):
    monkeypatch.setattr(kanban_warp, "launch_in_warp", lambda plan: pytest.fail("must not launch"))
    assert client.post("/api/plugins/kanban/tasks/t_missing/open-session").status_code == 404
