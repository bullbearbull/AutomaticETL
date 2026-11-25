import asyncio
import os
import subprocess
from pathlib import Path
from typing import ClassVar

import review
from comments import SUMMARY_MARKER
from reviewer import ReviewResult

DIFF = """\
diff --git a/a.py b/a.py
index 111..222 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 x = 1
+y = 2
 z = 3
"""
LOCK_ONLY = """\
diff --git a/poetry.lock b/poetry.lock
index 111..222 100644
--- a/poetry.lock
+++ b/poetry.lock
@@ -1 +1,2 @@
 a
+b
"""
USAGE = {
    "input_tokens": 11,
    "output_tokens": 22,
    "cache_read_input_tokens": 333,
    "cache_creation_input_tokens": 44,
}


class FakeClient:
    instances: ClassVar[list] = []

    def __init__(
        self,
        token,
        repo,
        pr,
        diff=DIFF,
        reviewed=False,
        previous=None,
        review_fails=False,
        diff_fails=False,
        existing=None,
        head_sha="sha1",
        base_sha="base0",
        next_head_sha=None,
    ):
        self.diff, self.reviewed, self.previous, self.review_fails = (
            diff,
            reviewed,
            previous,
            review_fails,
        )
        self.diff_fails, self.existing = diff_fails, existing or []
        self.head_sha, self.base_sha = head_sha, base_sha
        self.next_head_sha = next_head_sha
        self.get_pr_calls = 0
        self.summaries, self.reviews = [], []
        self.find_summary_calls = 0
        FakeClient.instances.append(self)

    def get_pr(self):
        self.get_pr_calls += 1
        sha = (
            self.next_head_sha if (self.next_head_sha and self.get_pr_calls > 1) else self.head_sha
        )
        return {"head": {"sha": sha}, "base": {"sha": self.base_sha}}

    def get_diff(self, base_sha=None, head_sha=None):
        if self.diff_fails:
            raise RuntimeError("406 diff too large")
        return self.diff

    def list_review_comments(self):
        return self.existing

    def find_summary_comment(self):
        self.find_summary_calls += 1
        if self.reviewed:
            return {
                "id": 1,
                "body": f"{SUMMARY_MARKER}\n<!-- reviewed-sha: {self.head_sha} -->\n완료",
            }
        return {"id": 1, "body": self.previous} if self.previous else None

    def upsert_summary(self, body):
        self.summaries.append(body)

    def post_review(self, sha, inline):
        if self.review_fails:
            raise RuntimeError("422")
        self.reviews.append(inline)
        return []


def setup(monkeypatch, tmp_path, result=None, **client_kw):
    FakeClient.instances = []
    monkeypatch.setattr(review, "GitHubClient", lambda *a, **k: FakeClient(*a, **client_kw))
    calls = []

    async def fake_run_review(prompt, options):
        calls.append(prompt)
        return result or ReviewResult(
            ok=True, summary="요약", findings=[], usage=USAGE, cost_usd=0.5
        )

    monkeypatch.setattr(review, "run_review", fake_run_review)
    for k, v in {
        "GITHUB_TOKEN": "t",
        "GITHUB_REPOSITORY": "o/r",
        "PR_NUMBER": "7",
        "GITHUB_WORKSPACE": str(tmp_path),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("PR_HEAD_SHA", raising=False)
    return calls


def test_empty_filtered_diff_skips_model_call(monkeypatch, tmp_path):
    calls = setup(monkeypatch, tmp_path, diff=LOCK_ONLY)
    assert review.main([]) == 0
    assert calls == [] and FakeClient.instances[0].summaries == []


def test_dry_run_prints_and_does_not_post(monkeypatch, tmp_path, capsys):
    calls = setup(
        monkeypatch,
        tmp_path,
        result=ReviewResult(
            ok=True,
            summary="요약 본문",
            usage=USAGE,
            findings=[{"path": "a.py", "line": 2, "severity": "major", "title": "T", "body": "B"}],
        ),
    )
    assert review.main(["--dry-run"]) == 0
    c = FakeClient.instances[0]
    assert len(calls) == 1 and c.summaries == [] and c.reviews == []
    out = capsys.readouterr().out
    assert "요약 본문" in out and "a.py" in out


def test_diff_file_mode_makes_no_github_calls(monkeypatch, tmp_path, capsys):
    calls = setup(monkeypatch, tmp_path)
    f = tmp_path / "x.diff"
    f.write_text(DIFF)
    assert review.main(["--diff-file", str(f)]) == 0
    assert len(calls) == 1 and FakeClient.instances == []
    assert "요약" in capsys.readouterr().out


def test_already_reviewed_sha_skips(monkeypatch, tmp_path):
    calls = setup(monkeypatch, tmp_path, reviewed=True)
    assert review.main([]) == 0
    c = FakeClient.instances[0]
    assert calls == [] and c.summaries == []
    assert c.find_summary_calls == 1


def test_summary_comment_lookup_is_not_duplicated_on_review(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    assert review.main([]) == 0
    assert FakeClient.instances[0].find_summary_calls == 1


def test_posts_inline_and_summary(monkeypatch, tmp_path):
    setup(
        monkeypatch,
        tmp_path,
        result=ReviewResult(
            ok=True,
            summary="요약",
            usage=USAGE,
            findings=[
                {"path": "a.py", "line": 2, "severity": "major", "title": "T", "body": "B"},
                {"path": "a.py", "line": 99, "severity": "minor", "title": "먼", "body": "B"},
            ],
        ),
    )
    assert review.main([]) == 0
    c = FakeClient.instances[0]
    assert [x["line"] for x in c.reviews[0]] == [2]
    assert (
        SUMMARY_MARKER in c.summaries[0]
        and "a.py:99" in c.summaries[0]
        and "reviewed-sha: sha1" in c.summaries[0]
    )


def test_inline_post_failure_moves_findings_to_summary(monkeypatch, tmp_path):
    setup(
        monkeypatch,
        tmp_path,
        review_fails=True,
        result=ReviewResult(
            ok=True,
            summary="요약",
            usage=USAGE,
            findings=[
                {"path": "a.py", "line": 2, "severity": "major", "title": "살려야 함", "body": "B"}
            ],
        ),
    )
    assert review.main([]) == 0
    assert "살려야 함" in FakeClient.instances[0].summaries[0]


def test_previous_successful_summary_is_passed_to_model(monkeypatch, tmp_path):
    prev = f"{SUMMARY_MARKER}\n<!-- reviewed-sha: old -->\n지난번 요약"
    calls = setup(monkeypatch, tmp_path, previous=prev)
    review.main([])
    assert "지난번 요약" in calls[0]


def test_previous_failure_comment_is_not_used_as_context(monkeypatch, tmp_path):
    prev = f"{SUMMARY_MARKER}\n<!-- review-failed-sha: old -->\n리뷰를 완료하지 못했습니다"
    calls = setup(monkeypatch, tmp_path, previous=prev)
    review.main([])
    assert "<previous_review>" not in calls[0]


def test_model_failure_posts_failure_comment_and_returns_zero(monkeypatch, tmp_path):
    setup(
        monkeypatch,
        tmp_path,
        result=ReviewResult(ok=False, error="error_max_budget_usd", usage=USAGE),
    )
    assert review.main([]) == 0
    body = FakeClient.instances[0].summaries[0]
    assert "error_max_budget_usd" in body and "reviewed-sha" not in body


def test_unexpected_exception_returns_zero(monkeypatch, tmp_path, capsys):
    setup(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(review, "parse_diff", boom)
    assert review.main([]) == 0
    assert "kaboom" in capsys.readouterr().err


def test_usage_written_to_step_summary(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    out = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(out))
    review.main([])
    text = out.read_text()
    for needle in ("11", "22", "333", "44", "0.5"):
        assert needle in text
    assert "cache" in text.lower()


def test_already_posted_inline_findings_are_not_reposted(monkeypatch, tmp_path):
    from comments import inline_comment

    f_same = {"path": "a.py", "line": 2, "severity": "major", "title": "T", "body": "B"}
    f_other_line = {"path": "a.py", "line": 3, "severity": "major", "title": "T", "body": "B"}
    f_outdated = {"path": "a.py", "line": 1, "severity": "minor", "title": "Old", "body": "B2"}
    existing = [
        {
            "path": "a.py",
            "line": 2,
            "body": inline_comment(f_same)["body"],
            "user": {"type": "Bot", "login": "github-actions[bot]"},
        },
        {
            "path": "a.py",
            "line": None,
            "body": inline_comment(f_outdated)["body"],
            "user": {"type": "Bot", "login": "github-actions[bot]"},
        },
    ]
    setup(
        monkeypatch,
        tmp_path,
        existing=existing,
        result=ReviewResult(
            ok=True, summary="요약", usage=USAGE, findings=[f_same, f_other_line, f_outdated]
        ),
    )
    assert review.main([]) == 0
    c = FakeClient.instances[0]
    assert [x["line"] for x in c.reviews[0]] == [3, 1]
    assert "a.py:2" not in c.summaries[0]


def test_stale_head_skips_posting_review_and_summary(monkeypatch, tmp_path):
    setup(
        monkeypatch,
        tmp_path,
        head_sha="1111111",
        next_head_sha="2222222",
        result=ReviewResult(
            ok=True,
            summary="요약",
            usage=USAGE,
            findings=[{"path": "a.py", "line": 2, "severity": "major", "title": "T", "body": "B"}],
        ),
    )
    assert review.main([]) == 0
    c = FakeClient.instances[0]
    assert c.reviews == [] and c.summaries == []


def test_github_token_not_visible_while_model_runs(monkeypatch, tmp_path):
    seen = {}
    setup(monkeypatch, tmp_path)

    async def spy(prompt, options):
        seen["token"] = os.environ.get("GITHUB_TOKEN")
        return ReviewResult(ok=True, summary="요약", usage=USAGE)

    monkeypatch.setattr(review, "run_review", spy)
    review.main([])
    assert seen["token"] is None


def test_model_workspace_includes_only_reviewable_tracked_files(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    setup(monkeypatch, repo)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "fixture.txt").write_text("fixture", encoding="utf-8")
    (repo / ".env.local").mkdir()
    (repo / ".env.local" / "fixture.txt").touch()
    (repo / "untracked.txt").touch()
    (repo / "alias.py").symlink_to("a.py")
    subprocess.run(
        ["git", "add", "-f", "a.py", "secrets/fixture.txt", ".env.local/fixture.txt", "alias.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    seen = []

    async def spy(prompt, options):
        workspace = Path(options.cwd)
        seen.append((workspace, sorted(p.name for p in workspace.iterdir())))
        assert (workspace / "a.py").read_text(encoding="utf-8") == "x = 1\n"
        return ReviewResult(ok=True, summary="요약", usage=USAGE)

    monkeypatch.setattr(review, "run_review", spy)
    assert review.main([]) == 0
    [(workspace, names)] = seen
    assert names == ["a.py"]
    assert not workspace.exists()


def test_model_timeout_posts_failure_comment(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "0.05")

    async def slow(prompt, options):
        await asyncio.sleep(5)

    monkeypatch.setattr(review, "run_review", slow)
    assert review.main([]) == 0
    body = FakeClient.instances[0].summaries[0]
    assert "timeout" in body and "reviewed-sha" not in body


def test_diff_fetch_failure_posts_failure_comment(monkeypatch, tmp_path):
    calls = setup(monkeypatch, tmp_path, diff_fails=True)
    assert review.main([]) == 0
    assert calls == []
    assert "diff" in FakeClient.instances[0].summaries[0]


def test_model_failure_preserves_previous_successful_summary(monkeypatch, tmp_path):
    prev = f"{SUMMARY_MARKER}\n<!-- reviewed-sha: oldsha -->\n## 🤖 자동 코드 리뷰\n이전 성공 내용"
    setup(
        monkeypatch,
        tmp_path,
        previous=prev,
        result=ReviewResult(ok=False, error="timeout", usage=USAGE),
    )
    assert review.main([]) == 0
    body = FakeClient.instances[0].summaries[0]
    assert "<!-- reviewed-sha: oldsha -->" in body
    assert "이전 성공 내용" in body
    assert "<!-- review-failed-sha: sha1 -->" in body
    assert "timeout" in body


def test_large_diff_api_failure_falls_back_to_local_git_diff(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "tester",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "tester",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "sample.py").write_text("a = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base"], cwd=repo, env=git_env, check=True, capture_output=True
    )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    (repo / "sample.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "head"], cwd=repo, env=git_env, check=True, capture_output=True
    )
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    calls = setup(
        monkeypatch,
        repo,
        diff_fails=True,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    assert review.main([]) == 0
    assert len(calls) == 1
    assert "+b = 2" in calls[0]
    assert f"<!-- reviewed-sha: {head_sha} -->" in FakeClient.instances[0].summaries[0]


def test_local_git_diff_does_not_fall_back_to_two_dot_when_merge_base_missing(tmp_path):
    repo = tmp_path / "orphan_repo"
    repo.mkdir()
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "tester",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "tester",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "root1"], cwd=repo, env=git_env, check=True, capture_output=True
    )
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    subprocess.run(
        ["git", "checkout", "--orphan", "unrelated"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "rm", "-rf", "."], cwd=repo, check=True, capture_output=True)
    (repo / "b.py").write_text("b = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "b.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "root2"], cwd=repo, env=git_env, check=True, capture_output=True
    )
    sha2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    assert review._local_git_diff(repo, sha1, sha2) is None


def test_skips_when_event_head_sha_mismatches_pr_head_sha(monkeypatch, tmp_path):
    calls = setup(monkeypatch, tmp_path, head_sha="newsha9999")
    monkeypatch.setenv("PR_HEAD_SHA", "oldsha1111")
    assert review.main([]) == 0
    assert calls == []
    assert FakeClient.instances[0].summaries == []
    assert FakeClient.instances[0].reviews == []
