"""Warp launch for "open session": the generated Tab Config runs the resolved command in the
resolved cwd, survives hostile quoting and task ids, lives under the user's
``~/.warp/tab_configs/``, and the launch itself only goes through the injected opener (tests
never open Warp)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

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


def _tab(text: str) -> dict:
    doc = tomllib.loads(text)
    (pane,) = doc["panes"]
    return {"name": doc["name"], "title": doc["title"], **pane}


def test_tab_config_runs_command_in_cwd(home):
    tab = _tab(kanban_warp.tab_config_toml(_plan()))

    assert tab["directory"] == "/work/repo"
    assert tab["commands"] == ["claude --resume s-1"]
    assert (tab["id"], tab["type"], tab["is_focused"]) == ("claude", "terminal", True)
    assert tab["name"] == "Hermes t_abc" and tab["title"] == "t_abc (resume)"


def test_tab_config_quotes_the_tmux_target_against_zsh_equals_expansion(home):
    tab = _tab(kanban_warp.tab_config_toml(_plan(mode="attach", argv=("tmux", "attach", "-t", "=hc-t_abc"))))
    assert tab["commands"] == ["tmux attach -t '=hc-t_abc'"]


def test_tab_config_survives_hostile_paths_and_ids(home):
    cwd = '/tmp/it\'s "a": dir # not a comment\\ \u00e9'
    plan = _plan(argv=("claude", "--resume", "x'; rm -rf ~ #"), cwd=cwd)

    tab = _tab(kanban_warp.tab_config_toml(plan))

    assert tab["directory"] == cwd
    assert tab["commands"] == ["claude --resume 'x'\"'\"'; rm -rf ~ #'"]


def test_tab_config_without_cwd_opens_in_home(home):
    tab = _tab(kanban_warp.tab_config_toml(_plan(mode="fresh", argv=("claude",), cwd=None)))
    assert tab["directory"] == str(home)


def test_write_tab_config_lands_in_user_warp_dir_and_is_overwritten(home):
    first = kanban_warp.write_tab_config(_plan())
    second = kanban_warp.write_tab_config(_plan(mode="attach", argv=("tmux", "attach", "-t", "=hc-t_abc")))

    assert first == second == home / ".warp" / "tab_configs" / "hermes_t_abc.toml"
    assert _tab(second.read_text())["commands"] == ["tmux attach -t '=hc-t_abc'"]


def test_odd_task_ids_get_a_snake_case_stem_matching_the_uri(home):
    path = kanban_warp.write_tab_config(_plan(task_id="t-1.b c"))

    assert path == home / ".warp" / "tab_configs" / "hermes_t_1_b_c.toml"
    assert kanban_warp.tab_config_uri("t-1.b c") == "warp://tab_config/hermes_t_1_b_c"
    assert _tab(path.read_text())["name"] == "Hermes t-1.b c"


@pytest.mark.platforms("macos")
def test_launch_opens_the_written_config_via_injected_opener(home, monkeypatch):
    monkeypatch.setattr(kanban_warp, "warp_app_path", lambda: Path("/Applications/Warp.app"))
    calls = []

    config = kanban_warp.launch_in_warp(_plan(), opener=lambda argv: calls.append(list(argv)) or True)

    assert config == home / ".warp" / "tab_configs" / "hermes_t_abc.toml"
    assert config.exists()
    assert len(calls) == 1 and calls[0][-1] == "warp://tab_config/hermes_t_abc"


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
