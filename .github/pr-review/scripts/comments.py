"""ReviewResult를 GitHub PR 요약·인라인·실패 코멘트 본문으로 변환한다."""

from __future__ import annotations

import re
from typing import Any

from diffutil import FileDiff, is_valid_line
from reviewer import ReviewResult

SUMMARY_MARKER = "<!-- claude-pr-review -->"
FAILURE_NOTICE_START = "<!-- review-failure-notice-start -->"
FAILURE_NOTICE_END = "<!-- review-failure-notice-end -->"
SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "nit": 3}
SEVERITY_LABEL = {
    "critical": "🔴 critical",
    "major": "🟠 major",
    "minor": "🟡 minor",
    "nit": "⚪ nit",
}
PREVIOUS_SUMMARY_LIMIT = 4000
MAX_COMMENT_LENGTH = 65_536
COMMENT_TRUNCATION_SUFFIX = "\n\n*(GitHub 코멘트 길이 제한으로 일부 내용이 생략되었습니다)*\n"

_FAILURE_NOTICE_RE = re.compile(
    re.escape(FAILURE_NOTICE_START) + r".*?" + re.escape(FAILURE_NOTICE_END),
    flags=re.S,
)


def _markers(sha: str) -> str:
    return f"{SUMMARY_MARKER}\n<!-- reviewed-sha: {sha} -->"


def has_reviewed_sha(comment_body: str | None, sha: str) -> bool:
    """요약 코멘트 본문에 지정 커밋 SHA의 완료 마커가 있는지 확인한다."""
    return comment_body is not None and f"<!-- reviewed-sha: {sha} -->" in comment_body


def _rank(finding: dict[str, Any]) -> int:
    sev = finding.get("severity")
    return SEVERITY_ORDER.get(str(sev), len(SEVERITY_ORDER))


def _cap_comment_body(body: str, limit: int = MAX_COMMENT_LENGTH) -> str:
    """GitHub 코멘트 길이 상한(65,536자)을 넘지 않도록 안전하게 자른다."""
    if len(body) <= limit:
        return body
    if limit <= len(COMMENT_TRUNCATION_SUFFIX):
        return body[:limit]
    head = body[: limit - len(COMMENT_TRUNCATION_SUFFIX)].rstrip()
    return head + COMMENT_TRUNCATION_SUFFIX


def strip_failure_notice(comment_body: str) -> str:
    """요약 코멘트 끝에 덧붙은 이전 실패 안내 블록을 제거한다."""
    return _FAILURE_NOTICE_RE.sub("", comment_body).rstrip()


def split_findings(
    result: ReviewResult, files: list[FileDiff]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """diff 유효 라인에 해당하는 지적과 요약으로 내릴 지적을 분리한다."""
    inline: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for f in result.findings:
        (inline if is_valid_line(files, f["path"], f["line"]) else demoted).append(f)
    return inline, demoted


def inline_comment(finding: dict[str, Any]) -> dict[str, Any]:
    """단일 지적을 GitHub PR review comment 페이로드로 변환한다."""
    label = SEVERITY_LABEL.get(finding["severity"], finding["severity"])
    body = _cap_comment_body(f"**[{label}] {finding['title']}**\n\n{finding['body']}")
    return {
        "path": finding["path"],
        "line": finding["line"],
        "side": "RIGHT",
        "body": body,
    }


def format_summary(
    result: ReviewResult,
    demoted: list[dict[str, Any]],
    omitted: list[str],
    sha: str,
) -> str:
    """요약 코멘트 본문을 생성하고 GitHub 길이 상한(65,536자) 이내로 보장한다."""
    parts = [_markers(sha), "## 🤖 자동 코드 리뷰", result.summary]
    if demoted:
        lines = ["### 라인에 직접 달 수 없는 지적"]
        for f in sorted(demoted, key=_rank):
            label = SEVERITY_LABEL.get(f["severity"], f["severity"])
            lines.append(f"- **[{label}]** `{f['path']}:{f['line']}` — {f['title']}\n  {f['body']}")
        parts.append("\n".join(lines))
    if omitted:
        parts.append(
            "### 크기 제한으로 리뷰하지 못한 파일\n" + "\n".join(f"- `{p}`" for p in omitted)
        )
    return _cap_comment_body("\n\n".join(parts) + "\n")


def format_failure(error: str, sha: str) -> str:
    """이전 성공 요약이 없을 때 사용하는 실패 안내 코멘트를 생성한다."""
    body = (
        f"{SUMMARY_MARKER}\n<!-- review-failed-sha: {sha} -->\n## 🤖 자동 코드 리뷰\n"
        f"리뷰를 완료하지 못했습니다 (`{error}`). 이 실패는 머지를 막지 않습니다. "
        "새 커밋을 푸시하거나 워크플로를 다시 실행하면 재시도합니다.\n"
    )
    return _cap_comment_body(body)


def format_failure_preserving_previous(
    existing_body: str | None,
    error: str,
    sha: str,
) -> str:
    """이전 성공 요약이 이미 있으면 본문과 ``reviewed-sha``를 보존한 채 실패 안내만 덧붙인다."""
    if not existing_body or "<!-- reviewed-sha:" not in existing_body:
        return format_failure(error, sha)

    notice = (
        f"{FAILURE_NOTICE_START}\n"
        f"<!-- review-failed-sha: {sha} -->\n"
        "---\n"
        f"> ⚠️ 최근 커밋(`{sha[:7]}`)의 자동 리뷰를 완료하지 못했습니다 (`{error}`). "
        "이전 리뷰 요약은 유지되며, 이 실패는 머지를 막지 않습니다. "
        "새 커밋을 푸시하거나 워크플로를 다시 실행하면 재시도합니다.\n"
        f"{FAILURE_NOTICE_END}\n"
    )
    base = strip_failure_notice(existing_body)
    combined = f"{base}\n\n{notice}"
    if len(combined) <= MAX_COMMENT_LENGTH:
        return combined
    # 실패 안내 블록과 상단 마커는 온전히 유지하고 이전 요약 본문 끝만 잘라낸다.
    available = MAX_COMMENT_LENGTH - len(notice) - 2
    if available <= 0:
        return _cap_comment_body(notice)
    trimmed_base = _cap_comment_body(base, limit=available).rstrip()
    return f"{trimmed_base}\n\n{notice}"


def extract_previous_summary(comment_body: str) -> str | None:
    """이전 성공 요약 코멘트에서 HTML 마커와 실패 안내를 제거한 본문만 추출한다."""
    if SUMMARY_MARKER not in comment_body:
        return None
    cleaned = strip_failure_notice(comment_body)
    text = re.sub(r"<!--.*?-->", "", cleaned, flags=re.S).strip()
    return text[:PREVIOUS_SUMMARY_LIMIT] or None
