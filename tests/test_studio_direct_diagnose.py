from __future__ import annotations

from types import SimpleNamespace

import pytest

from m365_copilot_openai_proxy import studio_diagnostics as _MODULE


def test_classify_upstream_error_uses_closed_categories_without_detail():
    assert _MODULE.classify_error("M365 Copilot refused this turn (agent=secret)") == "refused"
    assert _MODULE.classify_error("M365 Copilot returned an empty response twice") == "empty"
    assert _MODULE.classify_error("Upstream stopped sending data (idle timeout)") == "timeout"
    assert _MODULE.classify_error("Access token is not a substrate.office.com token") == "auth"
    assert _MODULE.classify_error("arbitrary private response body") == "other"


def test_public_result_has_no_raw_error_or_identifiers():
    result = _MODULE.public_result(
        category="refused",
        chunks=0,
        chars=0,
        agent_id="secret-agent-id",
        error_detail="private response body",
    )
    assert result == {"ok": False, "category": "refused", "chunks": 0, "chars": 0}


def test_runtime_config_matches_http_studio_resolution():
    class Key:
        tool_prompt = "key tool"
        system_prompt = "key system"
        time_zone = "Europe/London"
        ws_idle_timeout_minutes = 2

    runtime = {
        "tone_options": [{"value": "Gpt_5_6_Reasoning", "label": "gpt-5.6"}],
        "current_tone": "Magic",
        "global_tool_prompt": "global tool",
        "system_prompt": "global system",
        "time_zone": "Asia/Shanghai",
        "ws_idle_timeout_minutes": 5,
    }
    assert _MODULE.effective_runtime_config(Key(), runtime, model="gpt-5.6") == {
        "tone": "Gpt_5_6_Reasoning",
        "tool_prompt": "global tool\n\nkey tool",
        "system_override": "key system",
        "time_zone": "Europe/London",
        "idle_timeout": 120,
    }


@pytest.mark.parametrize("model", ["gpt-5.6-持续", " GPT-5.6:PERSIST ", "unknown"])
def test_runtime_config_reuses_http_tone_resolution(model):
    from m365_copilot_openai_proxy.routes_api_common import resolve_request_tone

    runtime = {
        "tone_options": [{"value": "Gpt_5_6_Reasoning", "label": "gpt-5.6"}],
        "current_tone": "Magic",
    }
    app = SimpleNamespace(state=SimpleNamespace(**runtime))
    expected, _ = resolve_request_tone(app, model)
    assert _MODULE.effective_runtime_config(None, runtime, model=model)["tone"] == expected


def test_runtime_config_uses_builtin_tones_like_http_when_options_are_absent():
    from m365_copilot_openai_proxy.routes_api_common import resolve_request_tone
    from m365_copilot_openai_proxy.tone_options import TONE_OPTIONS

    model = next(option["value"] for option in TONE_OPTIONS if option["value"] != "Magic")
    expected, _ = resolve_request_tone(SimpleNamespace(state=SimpleNamespace()), model)
    assert _MODULE.effective_runtime_config(None, {}, model=model)["tone"] == expected


def test_runtime_config_inherits_global_values_and_does_not_mutate_inputs():
    key = SimpleNamespace(tool_prompt=" ", system_prompt="", time_zone="", ws_idle_timeout_minutes=0)
    runtime = {
        "global_tool_prompt": " global tool ",
        "system_prompt": "global system",
        "time_zone": "UTC",
        "ws_idle_timeout_minutes": 3,
    }
    before_key, before_runtime = vars(key).copy(), runtime.copy()
    assert _MODULE.effective_runtime_config(key, runtime, model="unknown") == {
        "tone": "Magic", "tool_prompt": "global tool", "system_override": "global system",
        "time_zone": "UTC", "idle_timeout": 180,
    }
    assert vars(key) == before_key
    assert runtime == before_runtime
    assert _MODULE.effective_runtime_config(None, {}, model="unknown")["idle_timeout"] is None


@pytest.mark.parametrize("category,expected", [(None, None), ("timeout", "timeout"), ("private detail", "other")])
def test_public_result_keeps_closed_categories_and_nonnegative_counts(category, expected):
    assert _MODULE.public_result(category=category, chunks=-1, chars=-2) == {
        "ok": category is None, "category": expected, "chunks": 0, "chars": 0,
    }


def test_helpers_run_from_source_only_checkout_without_probe(tmp_path):
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    package = Path(_MODULE.__file__).parent
    shutil.copytree(package, tmp_path / package.name, ignore=shutil.ignore_patterns("__pycache__"))
    code = """
import pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
from m365_copilot_openai_proxy import studio_diagnostics as helpers
assert pathlib.Path(helpers.__file__).is_relative_to(root)
assert helpers.classify_error('private response') == 'other'
assert helpers.public_result(category=None, chunks=1, chars=1)['ok']
assert helpers.effective_runtime_config(None, {}, model='unknown')['tone'] == 'Magic'
assert 'run' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
