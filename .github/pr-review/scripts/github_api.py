"""PR 리뷰 조회·게시를 위한 최소 GitHub REST API 클라이언트."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import requests
from comments import SUMMARY_MARKER, has_reviewed_sha

PER_PAGE = 100
DEFAULT_BOT_LOGIN = "github-actions[bot]"


class GitHubClient:
    def __init__(
        self,
        token: str,
        repo: str,
        pr: int,
        session: Any = None,
        *,
        bot_login: str | None = None,
    ) -> None:
        self.repo, self.pr = repo, pr
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        self.bot_login = bot_login or os.environ.get("REVIEW_BOT_LOGIN", DEFAULT_BOT_LOGIN)
        self.session: Any = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
        )

    def _is_own_bot(self, comment: dict[str, Any]) -> bool:
        """이 워크플로의 봇 계정(기본 ``github-actions[bot]``)이 작성한 코멘트인지 판정한다."""
        user = comment.get("user")
        if not isinstance(user, dict):
            return False
        return user.get("type") == "Bot" and user.get("login") == self.bot_login

    def _request(
        self,
        method: str,
        path: str,
        *,
        ok_statuses: Sequence[int] = (),
        **kw: Any,
    ) -> Any:
        kw.setdefault("timeout", 30)
        resp = self.session.request(method, f"{self.base}/repos/{self.repo}{path}", **kw)
        if resp.status_code >= 400 and resp.status_code not in ok_statuses:
            raise RuntimeError(f"GitHub {method} {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp

    def get_pr(self) -> dict[str, Any]:
        """PR 메타데이터(head/base SHA 포함)를 조회한다."""
        data: dict[str, Any] = self._request(
            "GET", f"/pulls/{self.pr}", headers={"Accept": "application/vnd.github+json"}
        ).json()
        return data

    def get_diff(self, base_sha: str | None = None, head_sha: str | None = None) -> str:
        """PR unified diff 문자열을 조회한다.

        ``base_sha``와 ``head_sha``가 함께 주어지면 compare 엔드포인트로 해당 커밋 쌍에
        고정된 three-dot diff를 조회해 조회 중 head 변경으로 인한 커밋 불일치를 방지한다.
        """
        path = f"/compare/{base_sha}...{head_sha}" if base_sha and head_sha else f"/pulls/{self.pr}"
        text: str = self._request(
            "GET", path, headers={"Accept": "application/vnd.github.diff"}
        ).text
        return text

    def find_summary_comment(self) -> dict[str, Any] | None:
        """이 봇 계정이 작성한 요약 코멘트를 페이지네이션으로 탐색한다."""
        page = 1
        while True:
            items: list[dict[str, Any]] = self._request(
                "GET", f"/issues/{self.pr}/comments", params={"per_page": PER_PAGE, "page": page}
            ).json()
            for c in items:
                if SUMMARY_MARKER in c.get("body", "") and self._is_own_bot(c):
                    return c
            if len(items) < PER_PAGE:
                return None
            page += 1

    def list_review_comments(self) -> list[dict[str, Any]]:
        """중복 게시 방지를 위해 이 봇 계정이 작성한 기존 인라인 리뷰 코멘트를 조회한다."""
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            items: list[dict[str, Any]] = self._request(
                "GET", f"/pulls/{self.pr}/comments", params={"per_page": PER_PAGE, "page": page}
            ).json()
            found += [c for c in items if self._is_own_bot(c)]
            if len(items) < PER_PAGE:
                return found
            page += 1

    def already_reviewed(self, sha: str) -> bool:
        """지정 커밋 SHA가 이미 성공적으로 리뷰되었는지 확인한다."""
        existing = self.find_summary_comment()
        return existing is not None and has_reviewed_sha(str(existing.get("body", "")), sha)

    def upsert_summary(self, body: str) -> None:
        """기존 봇 요약 코멘트가 있으면 수정하고 없으면 새로 생성한다."""
        existing = self.find_summary_comment()
        if existing:
            self._request("PATCH", f"/issues/comments/{existing['id']}", json={"body": body})
        else:
            self._request("POST", f"/issues/{self.pr}/comments", json={"body": body})

    def post_review(self, sha: str, inline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """인라인 코멘트를 일괄 게시하고, 거부(422) 시 단건 폴백 후 실패 항목을 반환한다."""
        if not inline:
            return []
        resp = self._request(
            "POST",
            f"/pulls/{self.pr}/reviews",
            ok_statuses=(422,),
            json={"commit_id": sha, "event": "COMMENT", "body": "", "comments": inline},
        )
        if resp.status_code != 422:
            return []
        failed: list[dict[str, Any]] = []
        for c in inline:
            try:
                single = self._request(
                    "POST",
                    f"/pulls/{self.pr}/comments",
                    ok_statuses=(422,),
                    json={**c, "commit_id": sha},
                )
            except RuntimeError:
                failed.append(c)
                continue
            if single.status_code >= 300:
                failed.append(c)
        return failed
