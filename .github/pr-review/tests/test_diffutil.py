from diffutil import (
    FileDiff,
    filter_files,
    is_sensitive_path,
    is_valid_line,
    parse_diff,
    truncate_files,
)

MODIFIED = """\
diff --git a/app/main.py b/app/main.py
index 111..222 100644
--- a/app/main.py
+++ b/app/main.py
@@ -1,4 +1,5 @@
 import os
+import sys

 def f():
-    return 1
+    return 2
"""

RENAME_ONLY = """\
diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
"""

BINARY = """\
diff --git a/logo.png b/logo.png
index 111..222 100644
Binary files a/logo.png and b/logo.png differ
"""

SPACES_HANGUL = """\
diff --git "a/문서/내 파일.py" "b/문서/내 파일.py"
index 111..222 100644
--- "a/문서/내 파일.py"
+++ "b/문서/내 파일.py"
@@ -1,1 +1,2 @@
 a = 1
+b = 2
"""

DELETED = """\
diff --git a/gone.py b/gone.py
deleted file mode 100644
index 111..000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
"""


def fd(path, patch="@@ -1 +1 @@\n+x\n", status="modified", is_binary=False):
    return FileDiff(path=path, status=status, is_binary=is_binary, patch=patch, valid_lines={1})


def test_parse_modified_file_valid_lines():
    (f,) = parse_diff(MODIFIED)
    assert f.path == "app/main.py"
    assert f.status == "modified"
    assert f.valid_lines == {1, 2, 3, 4, 5}


def test_parse_rename_only_and_binary_do_not_crash():
    files = parse_diff(RENAME_ONLY + BINARY)
    by_path = {f.path: f for f in files}
    assert by_path["new.py"].status == "renamed"
    assert by_path["new.py"].valid_lines == set()
    assert by_path["logo.png"].is_binary is True


def test_parse_path_with_spaces_and_hangul():
    (f,) = parse_diff(SPACES_HANGUL)
    assert f.path == "문서/내 파일.py"
    assert is_valid_line([f], "문서/내 파일.py", 2)


def test_filter_drops_lockfiles_generated_binary_deleted():
    files = [
        fd("src/a.py"),
        fd("package-lock.json"),
        fd("poetry.lock"),
        fd("web/app.min.js"),
        fd("dist/bundle.js"),
        fd("api_pb2.py"),
        fd("logo.png", is_binary=True),
        fd("gone.py", status="deleted"),
        fd(".env"),
        fd("config/.env.production"),
        fd("secrets/token.json"),
        fd("app/secrets/cert.txt"),
        fd("certs/server.pem"),
        fd("keys/private.key"),
        fd("certs/store.p12"),
        fd("certs/client.pfx"),
        fd(".git/config"),
    ]
    assert [f.path for f in filter_files(files)] == ["src/a.py"]


def test_truncate_reports_omitted_paths():
    files = [fd("a.py", patch="x" * 60), fd("b.py", patch="y" * 60), fd("c.py", patch="z" * 60)]
    kept, omitted = truncate_files(files, max_chars=100)
    assert [f.path for f in kept] == ["a.py"]
    assert omitted == ["b.py", "c.py"]


def test_is_valid_line_false_for_unchanged_far_line():
    (f,) = parse_diff(MODIFIED)
    assert is_valid_line([f], "app/main.py", 5) is True
    assert is_valid_line([f], "app/main.py", 99) is False
    assert is_valid_line([f], "other.py", 1) is False


def test_truncate_cuts_single_oversized_file_instead_of_dropping_it():
    kept, omitted = truncate_files([fd("big.py", patch="x" * 500)], max_chars=100)
    assert [f.path for f in kept] == ["big.py"]
    assert len(kept[0].patch) <= 100 + len("\n[... truncated ...]")
    assert omitted == []


# Real `git diff` ends ---/+++ lines with a TAB when the path contains a space.
TAB_PATH = (
    "diff --git a/x y.py b/x y.py\n"
    "index 111..222 100644\n"
    "--- a/x y.py\t\n"
    "+++ b/x y.py\t\n"
    "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
)
QUOTED_TAB = (
    'diff --git "a/\\355\\225\\234 \\352\\270\\200.py" "b/\\355\\225\\234 \\352\\270\\200.py"\n'
    "index 111..222 100644\n"
    '--- "a/\\355\\225\\234 \\352\\270\\200.py"\t\n'
    '+++ "b/\\355\\225\\234 \\352\\270\\200.py"\t\n'
    "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
)


def test_parse_path_with_space_and_trailing_tab():
    (f,) = parse_diff(TAB_PATH)
    assert f.path == "x y.py"
    assert is_valid_line([f], "x y.py", 2)


def test_parse_octal_quoted_korean_path_with_space_and_trailing_tab():
    (f,) = parse_diff(QUOTED_TAB)
    assert f.path == "한 글.py"
    assert is_valid_line([f], "한 글.py", 2)


def test_parse_diff_preserves_line_numbers_with_form_feed_and_crlf():
    # 폼피드(\x0c)와 유니코드 줄구분자(\u2028)가 포함돼도 라인 번호가 밀리지 않아야 한다.
    diff = (
        "diff --git a/pdf.py b/pdf.py\r\n"
        "index 111..222 100644\r\n"
        "--- a/pdf.py\r\n"
        "+++ b/pdf.py\r\n"
        "@@ -1,2 +1,3 @@\r\n"
        ' a = "page1\x0cpage2"\r\n'
        '+b = "sep\u2028line"\r\n'
        " c = 3\r\n"
    )
    (f,) = parse_diff(diff)
    assert f.path == "pdf.py"
    assert f.valid_lines == {1, 2, 3}
    assert is_valid_line([f], "pdf.py", 3) is True
    assert is_valid_line([f], "pdf.py", 4) is False


def test_is_sensitive_path_blocks_git_and_bare_sensitive_dirs():
    for sensitive in (
        ".git",
        ".git/",
        ".git/config",
        ".git/HEAD",
        "sub/.git/HEAD",
        "secrets",
        "./secrets/",
        "sub/secrets",
        "secrets/a.txt",
        ".env.local",
        "a/cert.pem",
    ):
        assert is_sensitive_path(sensitive) is True, sensitive

    for safe in (
        "src/secrets_manager.py",
        "src/git_helper.py",
        ".github/workflows/ci.yml",
        "docs/readme.md",
    ):
        assert is_sensitive_path(safe) is False, safe
