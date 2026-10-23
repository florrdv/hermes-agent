"""Warp launch for "open session": the generated Launch Configuration runs the resolved
command in the resolved cwd, survives hostile quoting, lives under the user's
``~/.warp/launch_configurations/``, and the launch itself only goes through the injected
opener (tests never open Warp)."""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_yaml as yaml
from hermes_cli import kanban_warp
from hermes_cli.kanban_claude_session import OpenSessionPlan


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


def _plan(**kw) -> OpenSessionPlan:
    base = dict(task_id="t_abc", mode="resume", argv=("claude", "--resume", "s-1"), cwd="/work/repo", reason="r")
    return OpenSessionPlan(**{**base, **kw})


def _pane(text: str) -> dict:
    doc = yaml.safe_load(text)
    (window,) = doc["windows"]
    (tab,) = window["tabs"]
    return {"name": doc["name"], "title": tab["title"], **tab["layout"]}


def test_launch_config_runs_command_in_cwd(home):
    pane = _pane(kanban_warp.launch_config_yaml(_plan()))

    assert pane["cwd"] == "/work/repo"
    assert pane["commands"] == [{"exec": "claude --resume s-1"}]
    assert "t_abc" in pane["name"] and "resume" in pane["title"]


def test_launch_config_survives_hostile_paths_and_ids(home):
    cwd = '/tmp/it\'s "a": dir # not a comment'
    plan = _plan(argv=("claude", "--resume", "x'; rm -rf ~ #"), cwd=cwd)

    pane = _pane(kanban_warp.launch_config_yaml(plan))

    assert pane["cwd"] == cwd
    assert pane["commands"] == [{"exec": "claude --resume 'x'\"'\"'; rm -rf ~ #'"}]


def test_launch_config_without_cwd_opens_in_home(home):
    pane = _pane(kanban_warp.launch_config_yaml(_plan(mode="fresh", argv=("claude",), cwd=None)))
    assert pane["cwd"] == str(home)


def test_write_launch_config_lands_in_user_warp_dir_and_is_overwritten(home):
    first = kanban_warp.write_launch_config(_plan())
    second = kanban_warp.write_launch_config(_plan(mode="attach", argv=("tmux", "attach", "-t", "=hc-t_abc")))

    assert first == second == home / ".warp" / "launch_configurations" / "hermes-t_abc.yaml"
    assert _pane(second.read_text())["commands"] == [{"exec": "tmux attach -t =hc-t_abc"}]


def test_launch_uri_percent_encodes_the_path():
    assert (kanban_warp.launch_uri(Path("/Users/a b/.warp/launch_configurations/hermes-t_1.yaml"))
            == "warp://launch//Users/a%20b/.warp/launch_configurations/hermes-t_1.yaml")


@pytest.mark.platforms("macos")
def test_launch_opens_the_written_config_via_injected_opener(home, monkeypatch):
    monkeypatch.setattr(kanban_warp, "warp_app_path", lambda: Path("/Applications/Warp.app"))
    calls = []

    config = kanban_warp.launch_in_warp(_plan(), opener=lambda argv: calls.append(list(argv)) or True)

    assert config == home / ".warp" / "launch_configurations" / "hermes-t_abc.yaml"
    assert config.exists()
    assert len(calls) == 1 and calls[0][-1] == kanban_warp.launch_uri(config)


@pytest.mark.platforms("macos")
def test_launch_falls_back_to_naming_the_app_then_errors(home, monkeypatch):
    monkeypatch.setattr(kanban_warp, "warp_app_path", lambda: Path("/Applications/Warp.app"))
    calls = []

    with pytest.raises(kanban_warp.WarpUnavailable, match="failed"):
        kanban_warp.launch_in_warp(_plan(), opener=lambda argv: calls.append(list(argv)) and False)

    assert calls[1][-3:-1] == ["-a", "/Applications/Warp.app"]


@pytest.mark.platforms("macos")
def test_missing_warp_is_a_clear_error_and_writes_nothing(home, monkeypatch):
    monkeypatch.setattr(kanban_warp, "warp_app_path", lambda: None)

    with pytest.raises(kanban_warp.WarpUnavailable, match="Warp.app not found"):
        kanban_warp.launch_in_warp(_plan(), opener=lambda argv: pytest.fail("must not open"))
    assert not (home / ".warp").exists()


@pytest.mark.platforms("not macos")
def test_non_macos_is_a_clear_error(home):
    with pytest.raises(kanban_warp.WarpUnavailable, match="macOS only"):
        kanban_warp.launch_in_warp(_plan(), opener=lambda argv: pytest.fail("must not open"))
