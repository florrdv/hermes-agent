"""Kanban dashboard plugin: POST /tasks/{id}/open-session resolves the card's Claude Code
session and hands it to the Warp launcher (mocked here). Warp being unavailable is a 200
whose body carries the command for the user to run; an unknown task is a 404. GET /tasks/{id}
reports ``claude_session_available`` (recorded or inferable session) so the desktop drawer only
offers "Open Claude Code" on cards that had one."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_claude_session as kcs
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_warp
from hermes_cli.kanban_claude_transcripts import project_slug


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
                        lambda plan: launched.append(plan) or tmp_path / "hermes_x.toml")

    r = client.post(f"/api/plugins/kanban/tasks/{tid}/open-session")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["launched"] is True and body["error"] is None
    assert (body["mode"], body["command"], body["cwd"]) == ("resume", "claude --resume sess-7", str(tmp_path))
    assert [p.argv for p in launched] == [("claude", "--resume", "sess-7")]
    assert body["launch_config"] == str(tmp_path / "hermes_x.toml")


def test_open_session_attach_command_is_quoted_for_zsh(client, tmp_path, monkeypatch):
    tid = _task_with_session(tmp_path)
    monkeypatch.setattr(kcs, "tmux_session_alive", lambda name: True)
    monkeypatch.setattr(kanban_warp, "launch_in_warp", lambda plan: tmp_path / "hermes_x.toml")

    body = client.post(f"/api/plugins/kanban/tasks/{tid}/open-session").json()

    assert body["mode"] == "attach"
    assert body["command"] == f"tmux attach -t '=hc-{tid}'"


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


# --- GET /tasks/{id}: claude_session_available gates the drawer's "Open Claude Code" --------

def _task_detail(client, tid: str) -> dict:
    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200, r.text
    return r.json()


def _claimed_task(tmp_path) -> str:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder", workspace_kind="dir", workspace_path=str(tmp_path))
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
    return tid


def test_task_detail_reports_a_recorded_claude_session(client, tmp_path):
    assert _task_detail(client, _task_with_session(tmp_path))["claude_session_available"] is True


def test_task_detail_reports_a_recorded_tmux_session(client, tmp_path):
    tid = _claimed_task(tmp_path)
    with kbc.connect_closing() as conn:
        kcs.record_claude_session(conn, tid, tmux=f"hc-{tid}")

    assert _task_detail(client, tid)["claude_session_available"] is True


def test_task_detail_infers_a_session_from_the_latest_runs_transcript(client, tmp_path):
    tid = _claimed_task(tmp_path)
    project = tmp_path / ".claude" / "projects" / project_slug(str(tmp_path))
    project.mkdir(parents=True)
    (project / "sess-x.jsonl").write_text(json.dumps({"type": "user", "cwd": str(tmp_path)}) + "\n")

    assert _task_detail(client, tid)["claude_session_available"] is True


def test_task_detail_without_any_claude_session_is_unavailable(client, tmp_path):
    with kbc.connect_closing() as conn:
        never_ran = kb.create_task(conn, title="idle", workspace_kind="dir", workspace_path=str(tmp_path))

    assert _task_detail(client, never_ran)["claude_session_available"] is False
    assert _task_detail(client, _claimed_task(tmp_path))["claude_session_available"] is False
