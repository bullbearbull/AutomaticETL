from comments import (
    FAILURE_NOTICE_START,
    MAX_COMMENT_LENGTH,
    SUMMARY_MARKER,
    extract_previous_summary,
    format_failure,
    format_failure_preserving_previous,
    format_summary,
    inline_comment,
    split_findings,
)
from diffutil import FileDiff
from reviewer import ReviewResult


def finding(path="a.py", line=2, severity="major", title="제목", body="본문"):
    return {"path": path, "line": line, "severity": severity, "title": title, "body": body}


FILES = [FileDiff("a.py", "modified", False, "p", {1, 2, 3})]


def result(findings, summary="전체 요약"):
    return ReviewResult(ok=True, summary=summary, findings=findings)


def test_split_findings_demotes_invalid_lines():
    good, bad = finding(line=2), finding(line=99, title="먼 라인")
    other = finding(path="zzz.py", line=1, title="없는 파일")
    inline, demoted = split_findings(result([good, bad, other]), FILES)
    assert inline == [good]
    assert demoted == [bad, other]


def test_inline_comment_shape():
    c = inline_comment(finding(severity="critical", title="NPE", body="널 체크 필요"))
    assert c["path"] == "a.py" and c["line"] == 2 and c["side"] == "RIGHT"
    assert "NPE" in c["body"] and "널 체크 필요" in c["body"] and "critical" in c["body"].lower()


def test_format_summary_contains_both_markers_and_demoted_items():
    demoted = [
        finding(line=99, title="먼 라인", severity="minor"),
        finding(path="b.py", severity="critical", title="치명"),
    ]
    text = format_summary(result([]), demoted, ["big.py"], "abc123")
    assert SUMMARY_MARKER in text and "<!-- reviewed-sha: abc123 -->" in text
    assert "전체 요약" in text and "big.py" in text
    assert "a.py:99" in text and "b.py:2" in text
    assert text.index("치명") < text.index("먼 라인")  # sorted by severity


def test_format_failure_has_markers_and_reason():
    text = format_failure("error_max_budget_usd", "abc123")
    assert SUMMARY_MARKER in text and "error_max_budget_usd" in text
    # A failed review must not look "already reviewed", or a re-run would be skipped.
    assert "reviewed-sha" not in text


def test_extract_previous_summary_strips_markers():
    body = format_summary(result([]), [], [], "abc123")
    prev = extract_previous_summary(body)
    assert prev is not None
    assert "전체 요약" in prev and "<!--" not in prev
    assert extract_previous_summary("무관한 코멘트") is None


def test_format_summary_and_inline_respect_github_length_limit():
    huge = "가" * (MAX_COMMENT_LENGTH + 5000)
    summary = format_summary(result([], summary=huge), [], [], "abc123")
    assert len(summary) <= MAX_COMMENT_LENGTH
    assert SUMMARY_MARKER in summary
    assert "<!-- reviewed-sha: abc123 -->" in summary
    assert "길이 제한" in summary

    inline = inline_comment(finding(body=huge))
    assert len(inline["body"]) <= MAX_COMMENT_LENGTH
    assert "길이 제한" in inline["body"]


def test_format_failure_preserving_previous_keeps_success_and_deduplicates_notice():
    prev_ok = format_summary(result([], summary="이전 성공 요약"), [], [], "oldsha1")
    first_fail = format_failure_preserving_previous(prev_ok, "timeout", "newsha2")
    assert "<!-- reviewed-sha: oldsha1 -->" in first_fail
    assert "<!-- review-failed-sha: newsha2 -->" in first_fail
    assert "이전 성공 요약" in first_fail and "timeout" in first_fail
    assert len(first_fail) <= MAX_COMMENT_LENGTH

    # 반복 실패 시 실패 안내 블록이 중복 누적되지 않고 최신 실패로 교체된다.
    second_fail = format_failure_preserving_previous(first_fail, "error_max_turns", "newsha3")
    assert second_fail.count(FAILURE_NOTICE_START) == 1
    assert "<!-- review-failed-sha: newsha3 -->" in second_fail
    assert "newsha2" not in second_fail
    assert "이전 성공 요약" in second_fail

    # extract_previous_summary는 실패 안내 블록을 제외하고 이전 성공 본문만 돌려준다.
    extracted = extract_previous_summary(second_fail)
    assert extracted is not None
    assert "이전 성공 요약" in extracted
    assert "error_max_turns" not in extracted

    # 이전 성공 요약이 없으면 일반 실패 포맷을 반환한다.
    fresh_fail = format_failure_preserving_previous(None, "timeout", "sha0")
    assert "reviewed-sha" not in fresh_fail and "timeout" in fresh_fail
