"""Claude Code session recording + "open session" resolution for kanban cards.

Contracts: recorded fields merge into a run's metadata (never clobber it, and a later
completion handoff never erases them); the record CLI defaults task/run from the worker
env; resolution order is attach (live tmux) > resume (newest recorded session) > fresh,
with the resume cwd falling back to the task workspace.
"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_claude_session as kcs
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


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


def _claimed(conn, workspace: Path | None = None) -> tuple[str, int]:
    kwargs = {"workspace_kind": "dir", "workspace_path": str(workspace)} if workspace else {}
    tid = kb.create_task(conn, title="code it", assignee="claude-coder", **kwargs)
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    return tid, kb._current_run_id(conn, tid)


def _run_meta(conn, run_id: int) -> dict:
    return kb.get_run(conn, run_id).metadata or {}


def _cli(*argv: str) -> int:
    parser = argparse.ArgumentParser()
    kanban_cli.build_parser(parser.add_subparsers(dest="cmd"))
    return kanban_cli.kanban_command(parser.parse_args(["kanban", *argv]))


# --- Recording ---------------------------------------------------------------

def test_record_merges_into_existing_run_metadata(conn):
    tid, run_id = _claimed(conn)
    conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?", (json.dumps({"tests_run": 3}), run_id))

    kcs.record_claude_session(conn, tid, tmux="hc-custom")
    _, stored = kcs.record_claude_session(conn, tid, session_id="sess-1", cwd="/work/repo")

    assert stored == {"tests_run": 3, "tmux_session": "hc-custom", "claude_session_id": "sess-1",
                      "claude_cwd": "/work/repo"}
    assert _run_meta(conn, run_id) == stored


def test_record_requires_a_field_and_a_matching_run(conn):
    tid, _run_id = _claimed(conn)
    _other, other_run = _claimed(conn)
    with pytest.raises(ValueError, match="nothing to record"):
        kcs.record_claude_session(conn, tid)
    with pytest.raises(ValueError, match="does not belong"):
        kcs.record_claude_session(conn, tid, run_id=other_run, session_id="x")


def test_completion_metadata_merges_over_recorded_session(conn):
    tid, run_id = _claimed(conn)
    kcs.record_claude_session(conn, tid, session_id="sess-1", tmux="hc-x")

    assert kb.complete_task(conn, tid, result="done", metadata={"changed_files": ["a.py"]},
                            expected_run_id=run_id)

    assert _run_meta(conn, run_id) == {"claude_session_id": "sess-1", "tmux_session": "hc-x",
                                       "changed_files": ["a.py"]}


def test_record_cli_defaults_task_and_run_from_worker_env(conn, monkeypatch, tmp_path, capsys):
    tid, run_id = _claimed(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    workdir = tmp_path / "repo"
    workdir.mkdir()

    rc = _cli("claude-session", "record", "--session-id", "sess-9", "--cwd", str(workdir), "--json")

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["task_id"], out["run_id"]) == (tid, run_id)
    assert _run_meta(conn, run_id) == {"claude_session_id": "sess-9", "claude_cwd": str(workdir.resolve())}


def test_record_cli_without_task_outside_worker_is_usage_error(conn, capsys):
    assert _cli("claude-session", "record", "--session-id", "x") == 2
    assert "--task is required" in capsys.readouterr().err


# --- Resolution ----------------------------------------------------------------

def _plan(conn, tid, *, alive=()):
    return kcs.resolve_open_session(kb.get_task(conn, tid), kb.list_runs(conn, tid),
                                    tmux_alive=lambda name: name in alive)


def test_live_default_tmux_session_wins_over_recorded_claude_session(conn, tmp_path):
    tid, _ = _claimed(conn, workspace=tmp_path)
    kcs.record_claude_session(conn, tid, session_id="sess-1")

    plan = _plan(conn, tid, alive={f"hc-{tid}"})

    assert plan.mode == "attach"
    assert plan.argv == ("tmux", "attach", "-t", f"=hc-{tid}")


def test_recorded_tmux_name_is_probed_before_the_default(conn):
    tid, _ = _claimed(conn)
    kcs.record_claude_session(conn, tid, tmux="my session")

    plan = _plan(conn, tid, alive={"my session", f"hc-{tid}"})

    assert plan.mode == "attach" and plan.argv[-1] == "=my session"
    assert plan.command == "tmux attach -t '=my session'"


def test_resume_uses_newest_run_with_a_session_and_its_cwd(conn, tmp_path):
    workspace, old_cwd, new_cwd = (tmp_path / n for n in ("ws", "old", "new"))
    for d in (workspace, old_cwd, new_cwd):
        d.mkdir()
    tid, first_run = _claimed(conn, workspace=workspace)
    kcs.record_claude_session(conn, tid, session_id="old-sess", cwd=str(old_cwd))
    kb.complete_task(conn, tid, result="first pass", expected_run_id=first_run)
    second_run = kb._synthesize_ended_run(conn, tid, outcome="completed",
                                          metadata={"claude_session_id": "new-sess", "claude_cwd": str(new_cwd)})
    # A newer run without a session id must not hide the older one that has it.
    kb._synthesize_ended_run(conn, tid, outcome="completed", metadata={"note": "review"})

    plan = _plan(conn, tid)

    assert plan.mode == "resume"
    assert plan.argv == ("claude", "--resume", "new-sess")
    assert plan.cwd == str(new_cwd)
    assert f"run {second_run}" in plan.reason


def test_resume_cwd_falls_back_to_workspace_when_recorded_dir_is_gone(conn, tmp_path):
    tid, _ = _claimed(conn, workspace=tmp_path)
    kcs.record_claude_session(conn, tid, session_id="s", cwd=str(tmp_path / "deleted"))

    plan = _plan(conn, tid)

    assert (plan.mode, plan.cwd) == ("resume", str(tmp_path))
    assert "is gone" in plan.reason


def test_fresh_claude_in_workspace_when_nothing_recorded(conn, tmp_path):
    workspace = tmp_path / "it's here"
    workspace.mkdir()
    tid, _ = _claimed(conn, workspace=workspace)

    plan = _plan(conn, tid)

    assert (plan.mode, plan.argv, plan.cwd) == ("fresh", ("claude",), str(workspace))
    assert plan.shell_line == f"cd {shlex.quote(str(workspace))} && claude"


def test_open_session_print_cli_reports_plan_without_launching(conn, tmp_path, monkeypatch, capsys):
    tid, _ = _claimed(conn, workspace=tmp_path)
    kcs.record_claude_session(conn, tid, session_id="sess-1")
    monkeypatch.setattr(kcs, "tmux_session_alive", lambda name: False)
    launched = []
    from hermes_cli import kanban_warp
    monkeypatch.setattr(kanban_warp, "launch_in_warp", lambda plan: launched.append(plan))

    assert _cli("open-session", tid, "--print") == 0

    out = capsys.readouterr().out
    assert "mode:    resume" in out
    assert "command: claude --resume sess-1" in out
    assert f"cwd:     {tmp_path}" in out
    assert launched == []


def test_open_session_unknown_task_errors(conn, capsys):
    assert _cli("open-session", "t_nope", "--print") == 1
    assert "unknown task t_nope" in capsys.readouterr().err
