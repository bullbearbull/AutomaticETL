from comments import SUMMARY_MARKER
from github_api import GitHubClient


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data if data is not None else {}, text

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.headers = {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def client(responses):
    s = FakeSession(responses)
    return GitHubClient("tok", "o/r", 7, session=s), s


def comment(id_, body, user=None):
    if user is None:
        user = {"type": "Bot", "login": "github-actions[bot]"}
    return {"id": id_, "body": body, "user": user}


def test_upsert_updates_existing_comment_instead_of_creating():
    c, s = client([Resp(data=[comment(1, "x"), comment(2, f"{SUMMARY_MARKER}\nold")]), Resp()])
    c.upsert_summary(f"{SUMMARY_MARKER}\nnew")
    method, url, kw = s.calls[-1]
    assert (
        method == "PATCH"
        and url.endswith("/issues/comments/2")
        and kw["json"]["body"].endswith("new")
    )


def test_upsert_creates_when_absent():
    c, s = client([Resp(data=[]), Resp(status=201)])
    c.upsert_summary(f"{SUMMARY_MARKER}\nnew")
    method, url, _ = s.calls[-1]
    assert method == "POST" and url.endswith("/issues/7/comments")


def test_find_summary_comment_paginates():
    page1 = [comment(i, "x") for i in range(100)]
    c, s = client([Resp(data=page1), Resp(data=[comment(500, f"{SUMMARY_MARKER} hi")])])
    assert c.find_summary_comment()["id"] == 500
    assert len(s.calls) == 2
    assert [call[2]["params"]["page"] for call in s.calls] == [1, 2]


def test_already_reviewed_matches_sha_marker():
    body = f"{SUMMARY_MARKER}\n<!-- reviewed-sha: abc123 -->\nhi"
    c, _ = client([Resp(data=[comment(1, body)])])
    assert c.already_reviewed("abc123") is True
    c, _ = client([Resp(data=[comment(1, body)])])
    assert c.already_reviewed("def456") is False


def test_get_diff_uses_diff_media_type():
    c, s = client([Resp(text="DIFFTEXT")])
    assert c.get_diff() == "DIFFTEXT"
    assert s.calls[0][1].endswith("/pulls/7")
    assert s.calls[0][2]["headers"]["Accept"] == "application/vnd.github.diff"


def test_get_diff_with_shas_pins_compare_endpoint():
    c, s = client([Resp(text="PINNED")])
    assert c.get_diff("1111111", "2222222") == "PINNED"
    assert s.calls[0][1].endswith("/compare/1111111...2222222")
    assert s.calls[0][2]["headers"]["Accept"] == "application/vnd.github.diff"


def test_post_review_success_returns_no_failures():
    c, s = client([Resp(status=200)])
    inline = [{"path": "a.py", "line": 2, "side": "RIGHT", "body": "b"}]
    assert c.post_review("sha1", inline) == []
    body = s.calls[0][2]["json"]
    assert body["event"] == "COMMENT" and body["commit_id"] == "sha1" and body["comments"] == inline


def test_post_review_422_falls_back_to_single_comments():
    inline = [
        {"path": "a.py", "line": 2, "side": "RIGHT", "body": "1"},
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "2"},
        {"path": "a.py", "line": 4, "side": "RIGHT", "body": "3"},
    ]
    c, s = client([Resp(status=422), Resp(status=201), Resp(status=422), Resp(status=201)])
    failed = c.post_review("sha1", inline)
    assert failed == [inline[1]]
    assert [call[1].rsplit("/", 1)[-1] for call in s.calls[1:]] == ["comments"] * 3
    assert s.calls[1][2]["json"]["commit_id"] == "sha1"


def test_post_review_empty_inline_makes_no_call():
    c, s = client([])
    assert c.post_review("sha1", []) == [] and s.calls == []


def test_list_review_comments_paginates_and_keeps_own_bot_only():
    own = {"type": "Bot", "login": "github-actions[bot]"}
    other_bot = {"type": "Bot", "login": "other-app[bot]"}
    page1 = [{"id": i, "path": "a.py", "body": "x", "user": own} for i in range(100)]
    page2 = [
        {"id": 500, "path": "b.py", "body": "y", "user": {"type": "User", "login": "alice"}},
        {"id": 501, "path": "c.py", "body": "z", "user": own},
        {"id": 502, "path": "d.py", "body": "w", "user": other_bot},
        {"id": 503, "path": "e.py", "body": "q", "user": None},
    ]
    c, s = client([Resp(data=page1), Resp(data=page2)])
    got = c.list_review_comments()
    assert len(s.calls) == 2 and len(got) == 101
    assert [call[2]["params"]["page"] for call in s.calls] == [1, 2]
    assert all(x["user"]["login"] == "github-actions[bot]" for x in got)


def test_find_summary_comment_ignores_non_owner_bots_users_and_null_user():
    spoofed = [
        {"id": 1, "body": f"{SUMMARY_MARKER}\nfrom user", "user": {"type": "User", "login": "u"}},
        {
            "id": 2,
            "body": f"{SUMMARY_MARKER}\nfrom other bot",
            "user": {"type": "Bot", "login": "other-bot[bot]"},
        },
        {"id": 3, "body": f"{SUMMARY_MARKER}\nfrom ghost", "user": None},
        comment(4, f"{SUMMARY_MARKER}\nreal"),
    ]
    c, _ = client([Resp(data=spoofed)])
    found = c.find_summary_comment()
    assert found is not None and found["id"] == 4


def test_post_review_fallback_collects_non_422_errors_instead_of_raising():
    inline = [
        {"path": "a.py", "line": 2, "side": "RIGHT", "body": "1"},
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "2"},
    ]
    c, _ = client([Resp(status=422), Resp(status=500, text="boom"), Resp(status=201)])
    assert c.post_review("sha1", inline) == [inline[0]]
