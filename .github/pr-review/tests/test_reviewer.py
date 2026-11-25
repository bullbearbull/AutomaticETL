import asyncio
from pathlib import Path

import pytest
import reviewer
from claude_agent_sdk import ResultMessage
from reviewer import build_options, make_workspace_guard, run_review


def make_result(subtype="success", is_error=False, structured=None, **kw):
    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=5,
        is_error=is_error,
        num_turns=2,
        session_id="s",
        structured_output=structured,
        **kw,
    )


def fake_query(*messages, raises=None):
    async def _query(*, prompt, options=None, **_):
        for m in messages:
            yield m
        if raises:
            raise raises

    return _query


def opts():
    return build_options("SYS", Path("/tmp"), "claude-opus-5-5", 20, 2.0)


def test_build_options_tools_and_denies():
    o = opts()
    assert sorted(o.tools) == ["Glob", "Grep", "Read"]
    assert sorted(o.allowed_tools) == ["Glob", "Grep", "Read"]
    for t in ("Bash", "Write", "Edit", "WebFetch", "WebSearch"):
        assert t in o.disallowed_tools
    assert any(r.startswith("Read(") and ".env" in r for r in o.disallowed_tools)
    assert o.permission_mode == "dontAsk"
    assert o.setting_sources == []
    assert o.max_turns == 20 and o.max_budget_usd == 2.0
    assert o.model == "claude-opus-5-5"
    assert o.output_format["type"] == "json_schema"
    assert o.system_prompt == "SYS"


def test_run_review_success_maps_structured_output(monkeypatch):
    payload = {
        "summary": "좋음",
        "findings": [{"path": "a.py", "line": 1, "severity": "minor", "title": "t", "body": "b"}],
    }
    msg = make_result(
        structured=payload,
        total_cost_usd=0.12,
        usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 7,
        },
    )
    monkeypatch.setattr(reviewer, "query", fake_query(msg))
    r = asyncio.run(run_review("prompt", opts()))
    assert r.ok and r.summary == "좋음" and r.findings == payload["findings"]
    assert r.cost_usd == 0.12 and r.usage["cache_read_input_tokens"] == 100 and r.error is None


@pytest.mark.parametrize(
    "subtype", ["error_max_budget_usd", "error_max_turns", "error_max_structured_output_retries"]
)
def test_run_review_budget_exceeded_returns_not_ok(monkeypatch, subtype):
    monkeypatch.setattr(reviewer, "query", fake_query(make_result(subtype=subtype, is_error=True)))
    r = asyncio.run(run_review("prompt", opts()))
    assert not r.ok and r.error == subtype and r.findings == []


def test_run_review_exception_returns_not_ok(monkeypatch):
    monkeypatch.setattr(reviewer, "query", fake_query(raises=RuntimeError("boom")))
    r = asyncio.run(run_review("prompt", opts()))
    assert not r.ok and "boom" in r.error


def test_run_review_no_result_message_returns_not_ok(monkeypatch):
    monkeypatch.setattr(reviewer, "query", fake_query())
    r = asyncio.run(run_review("prompt", opts()))
    assert not r.ok and r.error


def test_build_options_denies_proc_sys_and_home():
    o = opts()
    for rule in (
        "Read(.env*)",
        "Read(**/*.p12)",
        "Read(**/*.pfx)",
        "Read(//proc/**)",
        "Read(//sys/**)",
        "Read(~/.*)",
        "Read(~/.*/**)",
        "Grep(.env*)",
        "Grep(**/secrets/**)",
        "Grep(//proc/**)",
        "Grep(~/.*)",
        "Grep(~/.*/**)",
    ):
        assert rule in o.disallowed_tools
    # GitHub Actions 워크스페이스(/home/runner/work/...)는 ~ 아래에 있으므로 ~/** 전체 거부는 금지
    assert "Read(~/**)" not in o.disallowed_tools
    assert "Grep(~/**)" not in o.disallowed_tools
    assert o.hooks and "PreToolUse" in o.hooks
    assert o.hooks["PreToolUse"][0].on_failure == "block"


def test_workspace_guard_allows_repo_files_and_blocks_outside_or_sensitive(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("top-secret\n", encoding="utf-8")
    link = repo / "escape.py"
    link.symlink_to(outside)

    guard = make_workspace_guard(repo)

    def check(tool_name, tool_input):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": "t1",
            "session_id": "s1",
            "transcript_path": "",
            "cwd": str(repo),
        }
        return asyncio.run(guard(payload, "t1", {"signal": None}))

    # 저장소 내부 일반 파일과 Grep 내용 검색 정규식(pattern)은 허용
    assert check("Read", {"file_path": "src/ok.py"}) == {}
    assert check("Read", {"file_path": str(repo / "src" / "ok.py")}) == {}
    assert check("Grep", {"pattern": "def ", "path": "src"}) == {}
    assert check("Grep", {"pattern": "/api/v1", "path": "src"}) == {}
    assert check("Grep", {"pattern": "~/**", "path": "src"}) == {}
    assert check("Grep", {"pattern": r"\.key", "path": "src"}) == {}
    assert check("Grep", {"pattern": ".env", "path": "src"}) == {}
    assert check("Grep", {"pattern": "secrets/", "path": "src"}) == {}
    assert check("Glob", {"pattern": "src/**/*.py"}) == {}
    assert check("Glob", {"pattern": "src/{api,lib}/**/*.py"}) == {}
    assert check("Grep", {"pattern": "class", "glob": "{src,tests}/**/*.py"}) == {}
    assert check("Glob", {"pattern": "**/*.md"}) == {}
    assert check("Glob", {"pattern": "src/**/*"}) == {}
    assert check("Grep", {"pattern": "def", "glob": "*.py"}) == {}
    assert check("Grep", {"pattern": "def", "glob": "tests/test_*.py"}) == {}

    # 저장소 외부·홈 숨김 경로·심볼릭 링크 이탈·저장소 내부 민감 경로는 모두 거부
    for tool, inp in (
        ("Read", {"file_path": "~/.azure/accessTokens.json"}),
        ("Read", {"file_path": "~/.pgpass"}),
        ("Read", {"file_path": "../secret.txt"}),
        ("Read", {"file_path": str(outside)}),
        ("Read", {"file_path": str(link)}),
        ("Read", {"file_path": "/proc/self/environ"}),
        ("Read", {"file_path": ".env"}),
        ("Read", {"file_path": "secrets/creds.json"}),
        ("Read", {"file_path": ".git/config"}),
        ("Read", {"file_path": ".git/HEAD"}),
        ("Grep", {"pattern": "x", "path": "~/.ssh"}),
        ("Grep", {"pattern": "x", "path": ".git"}),
        ("Grep", {"pattern": "x", "path": "secrets"}),
        ("Grep", {"pattern": "x", "glob": "../**/*.py"}),
        ("Grep", {"pattern": "x", "glob": ".env*"}),
        ("Grep", {"pattern": "x", "glob": "**/*.key"}),
        ("Grep", {"pattern": "x", "glob": "*.p?m"}),
        ("Grep", {"pattern": "x", "glob": "*.[k]ey"}),
        ("Grep", {"pattern": "x", "glob": "*.{key,py}"}),
        ("Grep", {"pattern": "x", "glob": "s?crets/*"}),
        ("Grep", {"pattern": "x", "glob": ".g?t/*"}),
        ("Grep", {"pattern": "x", "glob": "{/etc,src}/*"}),
        ("Grep", {"pattern": "x", "glob": "{~/.ssh,src}/*"}),
        ("Glob", {"pattern": "~/.aws/**"}),
        ("Glob", {"pattern": ".env*"}),
        ("Glob", {"pattern": "secrets"}),
        ("Glob", {"pattern": ".git/*"}),
        ("Glob", {"pattern": "**/.*"}),
        ("Glob", {"pattern": "*.*"}),
        ("Glob", {"pattern": "*.{pem,txt}"}),
    ):
        res = check(tool, inp)
        assert res["hookSpecificOutput"]["permissionDecision"] == "deny", (tool, inp)
