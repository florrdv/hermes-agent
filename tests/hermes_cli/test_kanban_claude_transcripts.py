""""Open session" infers the Claude Code session from its transcript when no id was recorded.

Contracts: precedence is attach > recorded resume > inferred resume > fresh; a transcript counts
only when the cwd recorded inside it is the workspace or under it (the project-dir slug is lossy)
and its mtime falls inside the latest run's window; two matches never guess, they fall back to
fresh and name both ids; unreadable transcripts are skipped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_claude_session as kcs
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_claude_transcripts import project_slug


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    with kbc.connect_closing() as c:
        yield c


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


def _claimed(conn, workspace: Path) -> tuple[str, kb.Run]:
    tid = kb.create_task(conn, title="code it", assignee="claude-coder",
                         workspace_kind="dir", workspace_path=str(workspace))
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    return tid, kb.get_run(conn, kb._current_run_id(conn, tid))


def _transcript(projects: Path, cwd: Path | str, session_id: str, *, mtime: float | None = None,
                slug_of: Path | str | None = None) -> Path:
    """A transcript as Claude Code writes it: a cwd-less queue line first, then entries with cwd."""
    project = projects / project_slug(str(slug_of or cwd))
    project.mkdir(exist_ok=True)
    path = project / f"{session_id}.jsonl"
    lines = [{"type": "queue-operation", "operation": "enqueue"},
             {"type": "user", "cwd": str(cwd), "sessionId": session_id}]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _plan(conn, tid, projects, *, alive=()):
    return kcs.resolve_open_session(kb.get_task(conn, tid), kb.list_runs(conn, tid),
                                    tmux_alive=lambda name: name in alive, claude_projects_dir=projects)


def test_single_transcript_in_run_window_resumes_inferred_in_its_cwd(conn, projects, workspace):
    tid, run = _claimed(conn, workspace)
    path = _transcript(projects, workspace, "sess-a")

    plan = _plan(conn, tid, projects)

    assert plan.mode == kcs.MODE_RESUME_INFERRED == "resume-inferred"
    assert plan.argv == ("claude", "--resume", "sess-a")
    assert plan.cwd == str(workspace)
    assert str(path) in plan.reason and "not recorded" in plan.reason
    assert f"run {run.id}" in plan.reason


def test_transcript_in_a_workspace_subdirectory_is_found(conn, projects, workspace):
    subdir = workspace / "hermes-agent"
    subdir.mkdir()
    tid, _ = _claimed(conn, workspace)
    _transcript(projects, subdir, "sess-sub")

    plan = _plan(conn, tid, projects)

    assert (plan.mode, plan.argv[-1], plan.cwd) == ("resume-inferred", "sess-sub", str(subdir))


def test_inferred_cwd_falls_back_to_workspace_when_recorded_cwd_is_gone(conn, projects, workspace):
    tid, _ = _claimed(conn, workspace)
    _transcript(projects, workspace / "deleted", "sess-gone")

    plan = _plan(conn, tid, projects)

    assert (plan.mode, plan.cwd) == ("resume-inferred", str(workspace))


def test_recorded_session_id_beats_transcripts(conn, projects, workspace):
    tid, _ = _claimed(conn, workspace)
    _transcript(projects, workspace, "sess-transcript")
    kcs.record_claude_session(conn, tid, session_id="sess-recorded")

    plan = _plan(conn, tid, projects)

    assert (plan.mode, plan.argv[-1]) == ("resume", "sess-recorded")


def test_live_tmux_beats_transcripts(conn, projects, workspace):
    tid, _ = _claimed(conn, workspace)
    _transcript(projects, workspace, "sess-a")

    plan = _plan(conn, tid, projects, alive={f"hc-{tid}"})

    assert plan.mode == "attach"


def test_transcript_outside_the_run_window_is_ignored(conn, projects, workspace):
    tid, run = _claimed(conn, workspace)
    _transcript(projects, workspace, "sess-old", mtime=run.started_at - 3600)

    plan = _plan(conn, tid, projects)

    assert plan.mode == "fresh"


def test_window_is_the_latest_run_not_an_earlier_one(conn, projects, workspace):
    tid, first = _claimed(conn, workspace)
    kb.complete_task(conn, tid, result="first pass", expected_run_id=first.id)
    conn.execute("UPDATE task_runs SET started_at = ?, ended_at = ? WHERE id = ?",
                 (first.started_at - 7200, first.started_at - 7000, first.id))
    kb._synthesize_ended_run(conn, tid, outcome="completed", metadata={"note": "review"})
    _transcript(projects, workspace, "sess-first-run", mtime=first.started_at - 7100)

    assert _plan(conn, tid, projects).mode == "fresh"


@pytest.mark.parametrize("outside", ["b/c", "b-x"], ids=["slug-collision", "sibling-prefix"])
def test_transcript_whose_recorded_cwd_is_outside_the_workspace_is_ignored(conn, projects, tmp_path, outside):
    # Workspace .../b-c and cwd .../b/c share a slug; .../b and .../b-x share a slug prefix.
    workspace = tmp_path / ("b-c" if outside == "b/c" else "b")
    workspace.mkdir()
    tid, _ = _claimed(conn, workspace)
    stranger = tmp_path / outside
    _transcript(projects, stranger, "sess-stranger", slug_of=stranger)
    assert any(p.name.startswith(project_slug(str(workspace))) for p in projects.iterdir())

    plan = _plan(conn, tid, projects)

    assert plan.mode == "fresh"


def test_two_matching_sessions_fall_back_to_fresh_naming_both(conn, projects, workspace):
    tid, _ = _claimed(conn, workspace)
    _transcript(projects, workspace, "sess-one")
    _transcript(projects, workspace, "sess-two")

    plan = _plan(conn, tid, projects)

    assert (plan.mode, plan.argv) == ("fresh", ("claude",))
    assert "2 Claude transcripts" in plan.reason
    assert "sess-one" in plan.reason and "sess-two" in plan.reason


def test_malformed_and_cwd_less_transcripts_are_skipped(conn, projects, workspace):
    tid, _ = _claimed(conn, workspace)
    project = projects / project_slug(str(workspace))
    project.mkdir()
    (project / "garbage.jsonl").write_bytes(b"\xff\xfe not json\n[1, 2]\n")
    (project / "no-cwd.jsonl").write_text(json.dumps({"type": "queue-operation"}) + "\n")
    _transcript(projects, workspace, "sess-good")

    plan = _plan(conn, tid, projects)

    assert (plan.mode, plan.argv[-1]) == ("resume-inferred", "sess-good")


def test_no_inference_without_an_existing_workspace(conn, projects, tmp_path):
    workspace = tmp_path / "gone"
    workspace.mkdir()
    tid, _ = _claimed(conn, workspace)
    _transcript(projects, workspace, "sess-a")
    workspace.rmdir()

    assert _plan(conn, tid, projects).mode == "fresh"


def test_open_session_reads_transcripts_from_the_home_claude_projects_dir(conn, projects, workspace, monkeypatch):
    tid, _ = _claimed(conn, workspace)
    _transcript(projects, workspace, "sess-home")
    monkeypatch.setattr(kcs, "tmux_session_alive", lambda name: False)

    result = kcs.open_session(conn, tid, launch=False)

    assert (result["mode"], result["command"]) == ("resume-inferred", "claude --resume sess-home")
