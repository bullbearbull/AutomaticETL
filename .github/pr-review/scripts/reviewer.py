"""Claude Agent SDK를 통해 읽기 전용 PR 코드 리뷰 에이전트를 실행한다."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query
from claude_agent_sdk.types import HookCallback, HookContext, HookInput, SyncHookJSONOutput
from diffutil import is_sensitive_path
from prompting import REVIEW_SCHEMA

ALLOWED_TOOLS = ["Read", "Grep", "Glob"]

# 에이전트가 절대 읽거나 검색하면 안 되는 민감 경로.
# 주의: GitHub Actions(HOME=/home/runner, GITHUB_WORKSPACE=/home/runner/work/...)나
# 로컬 홈 아래 체크아웃에서는 `~/**` 전체 거부가 저장소 코드 자체의 읽기를 막으므로
# 홈 아래 숨김 파일·숨김 디렉터리(`~/.*`, `~/.*/**`)를 통째로 거부하고,
# 추가로 PreToolUse 훅으로 작업 디렉터리 외부 경로와 심볼릭 링크 이탈을 원천 차단한다.
SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    ".env*",
    "**/.env*",
    "secrets/**",
    "**/secrets/**",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    ".git/**",
    ".git/config",
    "//proc/**",
    "//sys/**",
    "~/.*",
    "~/.*/**",
)

DENIED_TOOLS = [
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    *(f"Read({pat})" for pat in SENSITIVE_PATH_PATTERNS),
    *(f"Grep({pat})" for pat in SENSITIVE_PATH_PATTERNS),
]


@dataclass
class ReviewResult:
    ok: bool
    summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    error: str | None = None


def _deny_tool(reason: str) -> SyncHookJSONOutput:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


_SENSITIVE_GLOB_SAMPLES: tuple[str, ...] = (
    ".env",
    ".env.local",
    "x.pem",
    "x.key",
    "x.p12",
    "x.pfx",
    "secrets",
    ".git",
)
_INNER_BRACE_RE = re.compile(r"\{([^{}]*)\}")


def _has_unsafe_glob_pattern(pattern: str) -> bool:
    """전개된 패턴이 절대·홈 경로 또는 상위 디렉터리를 가리키는지 판정한다."""
    norm = pattern.strip().replace("\\", "/")
    return norm.startswith(("/", "~")) or any(part.strip() == ".." for part in norm.split("/"))


def _expand_glob_braces(pattern: str, *, max_variants: int = 32) -> list[str] | None:
    """``{a,b}`` 중괄호 글롭을 최대 ``max_variants``개까지 전개한다. 초과/불완전 구문은 None."""
    variants = [pattern]
    while any("{" in v or "}" in v for v in variants):
        next_variants: list[str] = []
        progressed = False
        for v in variants:
            m = _INNER_BRACE_RE.search(v)
            if m is None:
                return None
            progressed = True
            prefix = v[: m.start()]
            suffix = v[m.end() :]
            for alt in m.group(1).split(","):
                next_variants.append(f"{prefix}{alt}{suffix}")
                if len(next_variants) > max_variants:
                    return None
        if not progressed:
            return None
        variants = next_variants
    return variants


def _glob_may_match_sensitive(pattern: str) -> bool:
    """민감 경로와 매치되는 세그먼트를 거부한다. `*.*`도 키 확장자와 매치된다.

    재귀 검색의 파일 범위는 CLI가 준비한 비민감 작업 디렉터리로 제한한다.
    """
    norm = pattern.replace("\\", "/").lower()
    if is_sensitive_path(norm):
        return True
    for seg in norm.split("/"):
        if not seg or seg in {"*", "**"}:
            continue
        if any(fnmatch.fnmatch(sample, seg) for sample in _SENSITIVE_GLOB_SAMPLES):
            return True
    return False


def make_workspace_guard(cwd: Path) -> HookCallback:
    """``Read``·``Grep``·``Glob`` 호출이 저장소 내부 비민감 경로에만 머물도록 강제한다."""
    repo_root = cwd.resolve()

    async def _guard(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> SyncHookJSONOutput:
        if hook_input.get("hook_event_name") != "PreToolUse":
            return {}
        tool_name = str(hook_input.get("tool_name", ""))
        tool_input = hook_input.get("tool_input")
        if not isinstance(tool_input, dict):
            return {}

        path_key = "file_path" if tool_name == "Read" else "path"
        raw_path = tool_input.get(path_key)
        if isinstance(raw_path, str) and raw_path.strip():
            expanded = Path(raw_path).expanduser()
            candidate = (expanded if expanded.is_absolute() else (repo_root / expanded)).resolve()
            if not candidate.is_relative_to(repo_root):
                return _deny_tool("저장소 작업 디렉터리 외부 경로는 읽거나 검색할 수 없습니다.")
            rel = candidate.relative_to(repo_root).as_posix()
            if rel and rel != "." and is_sensitive_path(rel):
                return _deny_tool("민감 경로 파일은 읽거나 검색할 수 없습니다.")

        pat_key = "glob" if tool_name == "Grep" else ("pattern" if tool_name == "Glob" else None)
        if pat_key is not None:
            raw_pat = tool_input.get(pat_key)
            if isinstance(raw_pat, str) and raw_pat.strip():
                variants = _expand_glob_braces(raw_pat.strip())
                if variants is None or any(_has_unsafe_glob_pattern(p) for p in variants):
                    return _deny_tool("저장소 외부 경로를 가리키는 패턴은 허용되지 않습니다.")
                if any(_glob_may_match_sensitive(p) for p in variants):
                    return _deny_tool("민감 경로 패턴은 검색할 수 없습니다.")

        return {}

    return _guard


def build_options(
    system_prompt: str, cwd: Path, model: str, max_turns: int, max_budget_usd: float
) -> ClaudeAgentOptions:
    """읽기 전용 도구·민감 경로 거부·워크스페이스 경계 훅·비용/턴 상한 옵션을 구성한다."""
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        cwd=cwd,
        model=model,
        tools=list(ALLOWED_TOOLS),
        allowed_tools=list(ALLOWED_TOOLS),
        disallowed_tools=list(DENIED_TOOLS),
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="Read|Grep|Glob",
                    hooks=[make_workspace_guard(cwd)],
                    on_failure="block",
                )
            ]
        },
        permission_mode="dontAsk",
        setting_sources=[],
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        output_format={"type": "json_schema", "schema": REVIEW_SCHEMA},
    )


async def run_review(user_prompt: str, options: ClaudeAgentOptions) -> ReviewResult:
    """에이전트를 실행하고 구조화 출력·토큰 사용량·추정 비용을 ``ReviewResult``로 수집한다."""
    result: ResultMessage | None = None
    try:
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        return ReviewResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    if result is None:
        return ReviewResult(ok=False, error="no result message received")

    usage = dict(result.usage or {})
    cost_usd = result.total_cost_usd
    output = result.structured_output
    if result.subtype != "success" or result.is_error:
        error_code = (
            result.subtype if result.subtype != "success" else (result.stop_reason or "error")
        )
        return ReviewResult(ok=False, error=error_code, usage=usage, cost_usd=cost_usd)
    if not isinstance(output, dict) or "summary" not in output:
        return ReviewResult(
            ok=False, error="invalid structured output", usage=usage, cost_usd=cost_usd
        )
    raw_findings = output.get("findings", [])
    findings = list(raw_findings) if isinstance(raw_findings, list) else []
    return ReviewResult(
        ok=True,
        summary=str(output["summary"]),
        findings=findings,
        usage=usage,
        cost_usd=cost_usd,
    )
