"""Kanban dashboard plugin: GET /tasks/{id}/timeline returns the card's merged Live feed and a
cursor that makes the next poll return only newer entries; unknown task 404, bad cursor 400."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_timeline_test", plugin_file)
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


def test_timeline_feed_and_cursor(client, tmp_path):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default", workspace_kind="dir", workspace_path=str(tmp_path))
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
        kbd.heartbeat_worker(conn, tid, note="stage one")

    body = client.get(f"/api/plugins/kanban/tasks/{tid}/timeline").json()

    assert [(e["source"], e["kind"]) for e in body["entries"]] == [("card", "created"), ("card", "claimed"),
                                                                    ("stage", "note")]
    assert body["now"]["text"] == "stage one" and body["status"] == "running"

    with kbc.connect_closing() as conn:
        kb.add_comment(conn, tid, "flor", "hello")
    newer = client.get(f"/api/plugins/kanban/tasks/{tid}/timeline", params={"since": body["cursor"]}).json()

    assert [e["text"] for e in newer["entries"]] == ["flor: hello"]


def test_timeline_unknown_task_404_and_bad_cursor_400(client, tmp_path):
    assert client.get("/api/plugins/kanban/tasks/t_missing/timeline").status_code == 404
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
    assert client.get(f"/api/plugins/kanban/tasks/{tid}/timeline", params={"since": "@@"}).status_code == 400
