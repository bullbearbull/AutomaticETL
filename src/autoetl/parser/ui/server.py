"""무의존성 표준 라이브러리(`http.server`) 기반 Auto Parser 로컬 웹 UI 서버(`autoetl ui`).

보안 원칙:
- 기본 바인딩은 `127.0.0.1` 루프백으로 고정하며, 외부 네트워크 바인딩을 기본 차단한다.
- 업로드 파일은 요청 단위 임시 디렉터리에서 파싱 직후 삭제한다.
- 오류 응답에 문서 원문이나 스택 트레이스를 노출하지 않는다.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import secrets
import shutil
import tempfile
import threading
import time
import webbrowser
import zipfile
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from autoetl.doctor import collect_doctor_report
from autoetl.parser.adapters import ParseOptions, ParserInputError
from autoetl.parser.chunker import ChunkOptions, chunk_document
from autoetl.parser.exporters import (
    render_document_markdown,
    render_routing_report_markdown,
    render_table_markdown,
)
from autoetl.parser.layout_detector import create_layout_detector
from autoetl.parser.pipeline import parse_and_export, parse_page_selection, supported_extensions

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_REQUEST_BYTES = 70 * 1024 * 1024  # base64 ~50MB 원본 대응
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_CACHED_BUNDLES = 10
_MAX_INLINE_CROP_BYTES = 4 * 1024 * 1024

_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _load_ui_html() -> bytes:
    html_text = (
        resources.files("autoetl.parser.ui").joinpath("app.html").read_text(encoding="utf-8")
    )
    return html_text.encode("utf-8")


def _table_to_csv_text(headers: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    if headers:
        writer.writerow(headers)
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def _zip_directory(src_dir: Path, dst_zip: Path) -> None:
    with zipfile.ZipFile(dst_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src_dir).as_posix())


_MAX_CACHED_DOC_INDEXES = 6
_MAX_CACHED_RERANKERS = 2


class _BundleStore:
    """다운로드용 번들 ZIP 및 SQLite 인덱스 파일을 임시 디렉터리에 LRU 캐싱한다."""

    def __init__(self, max_items: int = _MAX_CACHED_BUNDLES) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="autoetl_ui_bundles_"))
        self._max_items = max_items
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[Path, str, str]] = OrderedDict()

    def _evict_locked(self) -> None:
        while len(self._items) > self._max_items:
            _old_tok, (old_path, _old_name, _old_ct) = self._items.popitem(last=False)
            old_path.unlink(missing_ok=True)

    def put_zip(self, bundle_dir: Path, download_name: str) -> str:
        token = secrets.token_hex(12)
        zip_path = self._dir / f"{token}.zip"
        _zip_directory(bundle_dir, zip_path)
        with self._lock:
            self._items[token] = (zip_path, download_name, "application/zip")
            self._evict_locked()
        return token

    def put_file(
        self,
        src_path: Path,
        download_name: str,
        *,
        content_type: str = "application/octet-stream",
    ) -> str:
        token = secrets.token_hex(12)
        ext = src_path.suffix.lower() if src_path.suffix else ".bin"
        dst_path = self._dir / f"{token}{ext}"
        shutil.copy2(src_path, dst_path)
        with self._lock:
            self._items[token] = (dst_path, download_name, content_type)
            self._evict_locked()
        return token

    def get(self, token: str) -> tuple[Path, str, str] | None:
        if not token or not all(ch in "0123456789abcdef" for ch in token):
            return None
        with self._lock:
            entry = self._items.get(token)
            if entry is None:
                return None
            file_path, dl_name, content_type = entry
            if not file_path.is_file():
                self._items.pop(token, None)
                return None
            self._items.move_to_end(token)
            return file_path, dl_name, content_type

    def close(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)


class _DocIndexStore:
    """로컬 웹 UI의 원문 문서 인덱스(`.sqlite`)와 선택 ONNX 리랭커 인스턴스를 LRU 관리한다."""

    def __init__(self, max_items: int = _MAX_CACHED_DOC_INDEXES) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="autoetl_ui_docindex_"))
        self._max_items = max_items
        self._lock = threading.Lock()
        self._items: OrderedDict[str, Path] = OrderedDict()
        self._rerankers: OrderedDict[str, Any] = OrderedDict()

    def allocate(self) -> tuple[str, Path]:
        token = secrets.token_hex(12)
        return token, self._dir / f"{token}.sqlite"

    def register(self, token: str, db_path: Path) -> None:
        with self._lock:
            self._items[token] = db_path
            while len(self._items) > self._max_items:
                _old_tok, old_path = self._items.popitem(last=False)
                old_path.unlink(missing_ok=True)

    def get(self, token: str) -> Path | None:
        if not token or not all(ch in "0123456789abcdef" for ch in token):
            return None
        with self._lock:
            db_path = self._items.get(token)
            if db_path is None:
                return None
            if not db_path.is_file():
                self._items.pop(token, None)
                return None
            self._items.move_to_end(token)
            return db_path

    def get_onnx_reranker(self, model_path_raw: str) -> Any:
        from autoetl.docindex.reranker import OnnxCrossEncoderReranker

        cleaned = model_path_raw.strip()
        if not cleaned:
            raise ParserInputError("ONNX 리랭커 모델 파일 경로(`reranker_model`)가 필요하다.")
        resolved = Path(cleaned).expanduser().resolve()
        if resolved.suffix.lower() != ".onnx":
            raise ParserInputError("리랭커 모델 파일은 `.onnx` 확장자여야 한다.")
        if not resolved.is_file():
            raise ParserInputError(f"ONNX 리랭커 모델 파일을 찾을 수 없다: {resolved.name}")
        key = str(resolved)
        with self._lock:
            cached = self._rerankers.get(key)
            if cached is not None:
                self._rerankers.move_to_end(key)
                return cached
        reranker = OnnxCrossEncoderReranker(resolved)
        reranker._ensure_ready()
        with self._lock:
            self._rerankers[key] = reranker
            while len(self._rerankers) > _MAX_CACHED_RERANKERS:
                self._rerankers.popitem(last=False)
        return reranker

    def close(self) -> None:
        with self._lock:
            self._rerankers.clear()
            self._items.clear()
        shutil.rmtree(self._dir, ignore_errors=True)


class _ParserUiHttpServer(ThreadingHTTPServer):
    """번들 스토어·문서 인덱스 스토어·HTML 캐시를 보유하는 `ThreadingHTTPServer` 확장."""

    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler]):
        self.ui_html_bytes = _load_ui_html()
        self.bundle_store = _BundleStore()
        self.docindex_store = _DocIndexStore()
        super().__init__(server_address, handler_cls)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.bundle_store.close()
            self.docindex_store.close()


class _ParserUiRequestHandler(BaseHTTPRequestHandler):
    """Auto Parser 로컬 웹 UI HTTP 요청 핸들러."""

    server: _ParserUiHttpServer

    def log_message(self, format: str, *args: object) -> None:
        # 문서 파일명·요청 본문이 콘솔 로그로 과도하게 남지 않도록 접근 로그 억제
        return

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, raw, content_type="application/json; charset=utf-8")

    def do_GET(self) -> None:
        path_only = self.path.split("?", 1)[0]
        if path_only in ("/", "/index.html"):
            self._send_bytes(
                HTTPStatus.OK,
                self.server.ui_html_bytes,
                content_type="text/html; charset=utf-8",
            )
            return

        if path_only == "/api/doctor":
            report = collect_doctor_report()
            self._send_json(HTTPStatus.OK, report.model_dump(mode="json"))
            return

        if path_only.startswith("/api/download/"):
            token = path_only[len("/api/download/") :].strip("/")
            found = self.server.bundle_store.get(token)
            if found is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "요청한 번들을 찾을 수 없다."})
                return
            file_path, dl_name, content_type = found
            safe_ascii = "".join(
                ch if (ch.isascii() and (ch.isalnum() or ch in "._-")) else "_" for ch in dl_name
            )
            if content_type == "application/zip" and not safe_ascii.endswith(".zip"):
                safe_ascii += ".zip"
            elif not Path(safe_ascii).suffix:
                safe_ascii += file_path.suffix or ".bin"
            self._send_bytes(
                HTTPStatus.OK,
                file_path.read_bytes(),
                content_type=content_type,
                extra_headers={"Content-Disposition": f'attachment; filename="{safe_ascii}"'},
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "알 수 없는 경로다."})

    def _read_json_body(self) -> dict[str, Any] | None:
        cl_str = self.headers.get("Content-Length", "0")
        if not cl_str.isdigit():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "유효하지 않은 Content-Length다."})
            return None
        content_len = int(cl_str)
        if content_len <= 0 or content_len > _MAX_REQUEST_BYTES:
            max_mb = _MAX_UPLOAD_BYTES // (1024 * 1024)
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": f"요청 본문 크기가 허용 상한({max_mb}MB)을 초과했다."},
            )
            return None
        try:
            raw_body = self.rfile.read(content_len)
            req = json.loads(raw_body.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "유효한 JSON 요청 본문이 아니다."})
            return None
        if not isinstance(req, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON 객체 형식이 필요하다."})
            return None
        return req

    def do_POST(self) -> None:
        path_only = self.path.split("?", 1)[0]
        if path_only == "/api/parse":
            req = self._read_json_body()
            if req is not None:
                self._handle_parse(req)
            return
        if path_only == "/api/analyze-tables":
            req = self._read_json_body()
            if req is not None:
                self._handle_analyze_tables(req)
            return
        if path_only == "/api/docindex/build":
            req = self._read_json_body()
            if req is not None:
                self._handle_docindex_build(req)
            return
        if path_only == "/api/docindex/query":
            req = self._read_json_body()
            if req is not None:
                self._handle_docindex_query(req)
            return
        if path_only == "/api/docindex/section":
            req = self._read_json_body()
            if req is not None:
                self._handle_docindex_section(req)
            return
        if path_only == "/api/docindex/table-sql":
            req = self._read_json_body()
            if req is not None:
                self._handle_docindex_table_sql(req)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "알 수 없는 API 경로다."})

    def _resolve_docindex_db(self, req: dict[str, Any]) -> Path | None:
        index_id = str(req.get("index_id", "")).strip()
        db_path = self.server.docindex_store.get(index_id)
        if db_path is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "문서 인덱스를 찾을 수 없다(만료되었거나 잘못된 index_id)."},
            )
            return None
        return db_path

    def _handle_docindex_build(self, req: dict[str, Any]) -> None:
        from autoetl.docindex import DocumentToolkit, build_document_index

        raw_files = req.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "인덱싱할 문서 파일 목록(`files`)이 1개 이상 필요하다."},
            )
            return

        allowed = supported_extensions()
        decoded_files: list[tuple[str, bytes]] = []
        total_bytes = 0
        seen_names: set[str] = set()

        for idx, item in enumerate(raw_files):
            if not isinstance(item, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "각 파일 항목은 `filename`과 `content_base64` 객체여야 한다."},
                )
                return
            filename = Path(str(item.get("filename", "")).strip()).name
            b64_data = item.get("content_base64")
            if not filename or not isinstance(b64_data, str) or not b64_data:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "각 항목에 파일명(`filename`)과 내용(`content_base64`)이 필요하다."},
                )
                return
            ext = Path(filename).suffix.lower()
            if ext not in allowed:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": f"지원하지 않는 문서 파일 형식이다: '{ext or '(없음)'}' "
                        f"(지원: {', '.join(allowed)})"
                    },
                )
                return
            try:
                fbytes = base64.b64decode(b64_data, validate=True)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "파일 Base64 디코딩에 실패했다."})
                return
            total_bytes += len(fbytes)
            if total_bytes > _MAX_UPLOAD_BYTES:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "업로드 파일 합계 크기가 50MB 상한을 초과했다."},
                )
                return
            safe_name = filename
            if safe_name in seen_names:
                safe_name = f"{idx + 1}_{filename}"
            seen_names.add(safe_name)
            decoded_files.append((safe_name, fbytes))

        t0 = time.perf_counter()
        index_id, db_path = self.server.docindex_store.allocate()
        try:
            with tempfile.TemporaryDirectory(prefix="autoetl_ui_docbuild_") as td:
                in_dir = Path(td) / "docs"
                in_dir.mkdir()
                for fname, fbytes in decoded_files:
                    (in_dir / fname).write_bytes(fbytes)

                summary = build_document_index(in_dir, db_path)
                self.server.docindex_store.register(index_id, db_path)
                download_token = self.server.bundle_store.put_file(
                    db_path,
                    "autoetl_docindex.sqlite",
                    content_type="application/vnd.sqlite3",
                )
                with DocumentToolkit(db_path) as kit:
                    outline_res = kit.get_outline()
                    tables_res = kit.list_tables()

            self._send_json(
                HTTPStatus.OK,
                {
                    "index_id": index_id,
                    "download_token": download_token,
                    "elapsed_seconds": round(time.perf_counter() - t0, 3),
                    "summary": summary.model_dump(mode="json"),
                    "documents": outline_res["documents"],
                    "sections": outline_res["sections"],
                    "tables": tables_res["tables"],
                },
            )
        except ParserInputError as exc:
            db_path.unlink(missing_ok=True)
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
        except Exception:
            db_path.unlink(missing_ok=True)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": (
                        "문서 인덱스 구축 중 내부 오류가 발생했다"
                        "(문서 본문 보호를 위해 상세 원문 생략)."
                    )
                },
            )

    def _handle_docindex_query(self, req: dict[str, Any]) -> None:
        from autoetl.docindex import DocumentToolkit

        db_path = self._resolve_docindex_db(req)
        if db_path is None:
            return
        query_text = str(req.get("query", "")).strip()
        if not query_text:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "검색 질의(`query`)가 필요하다."})
            return
        try:
            top_k = max(1, min(20, int(req.get("top_k", 5))))
        except (TypeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`top_k`는 정수여야 한다."})
            return
        doc_id_raw = req.get("doc_id")
        doc_id = (
            str(doc_id_raw).strip() if isinstance(doc_id_raw, str) and doc_id_raw.strip() else None
        )
        reranker_kind = str(req.get("reranker", "none") or "none").strip().lower()
        reranker_model = str(req.get("reranker_model", "") or "").strip()
        synonyms_raw = req.get("synonyms")
        synonyms: dict[str, list[str]] | None = None
        if isinstance(synonyms_raw, dict):
            synonyms = {
                str(k).strip(): (
                    [str(v).strip()]
                    if isinstance(v, str) and v.strip()
                    else [str(x).strip() for x in v if str(x).strip()]
                    if isinstance(v, list)
                    else []
                )
                for k, v in synonyms_raw.items()
                if str(k).strip()
            }
            synonyms = {k: v for k, v in synonyms.items() if v} or None
        elif isinstance(synonyms_raw, str) and synonyms_raw.strip():
            parsed_syn: dict[str, list[str]] = {}
            for entry in synonyms_raw.split(","):
                if "=" not in entry:
                    continue
                left, right = entry.split("=", 1)
                key = left.strip()
                vals = [v.strip() for v in right.split("|") if v.strip()]
                if key and vals:
                    parsed_syn.setdefault(key, []).extend(vals)
            synonyms = parsed_syn or None

        try:
            reranker_obj: Any = None
            if reranker_kind == "onnx":
                reranker_obj = self.server.docindex_store.get_onnx_reranker(reranker_model)
            elif reranker_kind != "none":
                raise ParserInputError(
                    f"지원하지 않는 리랭커 유형이다: '{reranker_kind}' (지원: none, onnx)"
                )

            with DocumentToolkit(db_path, reranker=reranker_obj) as kit:
                pack = kit.search_documents(
                    query_text,
                    top_k=top_k,
                    doc_id=doc_id,
                    synonyms=synonyms,
                )
            self._send_json(HTTPStatus.OK, pack)
        except ParserInputError as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": (
                        "문서 검색 중 내부 오류가 발생했다(문서 본문 보호를 위해 상세 원문 생략)."
                    )
                },
            )

    def _handle_docindex_section(self, req: dict[str, Any]) -> None:
        from autoetl.docindex import DocumentToolkit

        db_path = self._resolve_docindex_db(req)
        if db_path is None:
            return
        section_id = str(req.get("section_id", "")).strip()
        if not section_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`section_id`가 필요하다."})
            return
        try:
            with DocumentToolkit(db_path) as kit:
                content = kit.read_section(section_id)
            self._send_json(HTTPStatus.OK, content)
        except ParserInputError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "원문 섹션 조회 중 내부 오류가 발생했다."},
            )

    def _handle_docindex_table_sql(self, req: dict[str, Any]) -> None:
        from autoetl.docindex import DocumentToolkit

        db_path = self._resolve_docindex_db(req)
        if db_path is None:
            return
        table_uid = str(req.get("table_uid", "")).strip()
        sql = str(req.get("sql", "SELECT * FROM tbl LIMIT 50")).strip()
        if not table_uid:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`table_uid`가 필요하다."})
            return
        try:
            with DocumentToolkit(db_path) as kit:
                result = kit.query_table(table_uid, sql)
            self._send_json(HTTPStatus.OK, result)
        except ParserInputError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "표 SQL 실행 중 내부 오류가 발생했다."},
            )

    def _handle_analyze_tables(self, req: dict[str, Any]) -> None:
        from autoetl.contracts import Purpose
        from autoetl.core.load_planner import render_analysis_report
        from autoetl.core.multi_analyze import analyze_tables
        from autoetl.core.multi_context import SUPPORTED_TABLE_EXTENSIONS
        from autoetl.core.pipeline import InputError

        raw_files = req.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "분석할 테이블 파일 목록(`files`)이 1개 이상 필요하다."},
            )
            return

        raw_opts = req.get("options")
        opts_in: dict[str, Any] = raw_opts if isinstance(raw_opts, dict) else {}
        purpose_raw = str(opts_in.get("purpose", Purpose.OLTP.value) or Purpose.OLTP.value).lower()
        try:
            purpose = Purpose(purpose_raw)
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"지원하지 않는 분석 목적(`purpose`)이다: '{purpose_raw}'"},
            )
            return
        decompose_strings = bool(opts_in.get("decompose_strings", True))

        decoded_files: list[tuple[str, bytes]] = []
        total_bytes = 0
        seen_names: set[str] = set()
        allowed_exts = sorted(SUPPORTED_TABLE_EXTENSIONS)

        for idx, item in enumerate(raw_files):
            if not isinstance(item, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "각 파일 항목은 `filename`과 `content_base64` 객체여야 한다."},
                )
                return
            filename = Path(str(item.get("filename", "")).strip()).name
            b64_data = item.get("content_base64")
            if not filename or not isinstance(b64_data, str) or not b64_data:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "각 항목에 파일명(`filename`)과 내용(`content_base64`)이 필요하다."},
                )
                return
            ext = Path(filename).suffix.lower()
            if ext not in SUPPORTED_TABLE_EXTENSIONS:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": f"지원하지 않는 테이블 파일 형식이다: '{ext or '(없음)'}' "
                        f"(지원: {', '.join(allowed_exts)})"
                    },
                )
                return
            try:
                fbytes = base64.b64decode(b64_data, validate=True)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "파일 Base64 디코딩에 실패했다."})
                return
            total_bytes += len(fbytes)
            if total_bytes > _MAX_UPLOAD_BYTES:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "업로드 파일 합계 크기가 50MB 상한을 초과했다."},
                )
                return
            safe_name = filename
            if safe_name in seen_names:
                safe_name = f"{idx + 1}_{filename}"
            seen_names.add(safe_name)
            decoded_files.append((safe_name, fbytes))

        t0 = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="autoetl_ui_tables_") as td:
                td_path = Path(td)
                in_dir = td_path / "inputs"
                in_dir.mkdir()
                bundle_dir = td_path / "bundle"
                for fname, fbytes in decoded_files:
                    (in_dir / fname).write_bytes(fbytes)

                bundle = analyze_tables(
                    sources=[in_dir],
                    purpose=purpose,
                    decompose_strings=decompose_strings,
                    out_dir=bundle_dir,
                )
                bundle_id = self.server.bundle_store.put_zip(
                    bundle_dir,
                    "autoetl_multi_table_plan.zip",
                )
                report_md = render_analysis_report(bundle)

                payload: dict[str, Any] = {
                    "bundle_id": bundle_id,
                    "elapsed_seconds": round(time.perf_counter() - t0, 3),
                    "purpose": bundle.purpose.value,
                    "selected_candidate_id": bundle.load_plan.selected_candidate_id,
                    "tables": [tb.model_dump(mode="json") for tb in bundle.tables],
                    "relations": bundle.relations.model_dump(mode="json"),
                    "load_plan": bundle.load_plan.model_dump(mode="json"),
                    "analysis_report_markdown": report_md,
                    "warnings": list(bundle.warnings),
                }
            self._send_json(HTTPStatus.OK, payload)
        except (InputError, ValueError) as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": (
                        "다중 테이블 분석 중 내부 오류가 발생했다"
                        "(데이터 보호를 위해 상세 원문 생략)."
                    )
                },
            )

    def _handle_parse(self, req: dict[str, Any]) -> None:
        filename = Path(str(req.get("filename", "")).strip()).name
        b64_data = req.get("content_base64")
        raw_opts = req.get("options")
        opts_in: dict[str, Any] = raw_opts if isinstance(raw_opts, dict) else {}

        if not filename or not isinstance(b64_data, str) or not b64_data:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "파일명(`filename`)과 파일 내용(`content_base64`)이 필요하다."},
            )
            return

        ext = Path(filename).suffix.lower()
        if ext not in supported_extensions():
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": f"지원하지 않는 파일 형식이다: '{ext or '(없음)'}' "
                    f"(지원: {', '.join(supported_extensions())})"
                },
            )
            return

        try:
            file_bytes = base64.b64decode(b64_data, validate=True)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "파일 Base64 디코딩에 실패했다."})
            return

        if len(file_bytes) > _MAX_UPLOAD_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "업로드 파일 크기가 50MB 상한을 초과했다."},
            )
            return

        t0 = time.perf_counter()
        try:
            pages_spec = opts_in.get("pages")
            pages = parse_page_selection(str(pages_spec)) if pages_spec else None
            include_comments = bool(opts_in.get("include_comments", True))
            extract_crops = bool(opts_in.get("extract_crops", True))
            include_chunks = bool(opts_in.get("include_chunks", True))
            raw_chunk_max = opts_in.get("chunk_max_chars", 1500)
            raw_chunk_overlap = opts_in.get("chunk_overlap", 150)
            try:
                chunk_max_val = 1500 if raw_chunk_max is None else int(raw_chunk_max)
                chunk_overlap_val = 150 if raw_chunk_overlap is None else int(raw_chunk_overlap)
            except (TypeError, ValueError) as exc:
                raise ParserInputError(
                    "청크 옵션(`chunk_max_chars`, `chunk_overlap`)은 정수여야 한다."
                ) from exc
            chunk_max = max(200, min(20000, chunk_max_val))
            chunk_overlap = max(0, min(2000, chunk_overlap_val))
            layout_kind = str(opts_in.get("layout_detector", "none") or "none").lower()
            if layout_kind not in ("none", "heuristic"):
                layout_kind = "none"
            layout_detector = (
                create_layout_detector("heuristic") if layout_kind == "heuristic" else None
            )

            parse_opts = ParseOptions(
                pages=pages,
                extract_crops=extract_crops,
                include_comments=include_comments,
            )
            chunk_opts = ChunkOptions(max_chars=chunk_max, overlap_chars=chunk_overlap)

            with tempfile.TemporaryDirectory(prefix="autoetl_ui_req_") as td:
                td_path = Path(td)
                src_path = td_path / filename
                src_path.write_bytes(file_bytes)
                bundle_dir = td_path / "bundle"

                doc, _manifest = parse_and_export(
                    src_path,
                    bundle_dir,
                    options=parse_opts,
                    layout_detector=layout_detector,
                    include_chunks=include_chunks,
                    chunk_options=chunk_opts,
                )

                bundle_id = self.server.bundle_store.put_zip(
                    bundle_dir,
                    f"{src_path.stem}_autoetl_bundle.zip",
                )

                chunks = chunk_document(doc, options=chunk_opts) if include_chunks else []
                tables_out: list[dict[str, Any]] = [
                    {
                        "table_id": t.table_id,
                        "page": t.page,
                        "caption": t.caption,
                        "strategy": t.strategy.value,
                        "row_count": t.row_count,
                        "col_count": t.col_count,
                        "headers": t.headers,
                        "rows": t.rows,
                        "csv": _table_to_csv_text(t.headers, t.rows),
                        "markdown": render_table_markdown(t),
                    }
                    for t in doc.tables
                ]

                visuals_out: list[dict[str, Any]] = []
                for v in doc.visual_regions:
                    data_url: str | None = None
                    if v.crop_path:
                        cp = Path(v.crop_path)
                        if not cp.is_file():
                            cp = bundle_dir / v.crop_path
                        if cp.is_file() and cp.stat().st_size <= _MAX_INLINE_CROP_BYTES:
                            mime = _MIME_BY_EXT.get(cp.suffix.lower())
                            if mime:
                                b64_img = base64.b64encode(cp.read_bytes()).decode("ascii")
                                data_url = f"data:{mime};base64,{b64_img}"
                    visuals_out.append(
                        {
                            "region_id": v.region_id,
                            "page": v.page,
                            "element_kind": v.element_kind.value,
                            "recommended_strategy": v.recommended_strategy.value,
                            "reason": v.reason,
                            "caption": v.caption,
                            "crop_status": v.crop_status.value,
                            "image_data_url": data_url,
                        }
                    )

                response_payload: dict[str, Any] = {
                    "bundle_id": bundle_id,
                    "elapsed_seconds": round(time.perf_counter() - t0, 3),
                    "document": doc.document.model_dump(mode="json"),
                    "routing_plan": doc.routing_plan.model_dump(mode="json"),
                    "markdown": render_document_markdown(doc),
                    "routing_report_markdown": render_routing_report_markdown(doc.routing_plan),
                    "tables": tables_out,
                    "chunks": [c.model_dump(mode="json") for c in chunks],
                    "visuals": visuals_out,
                    "warnings": list(doc.warnings),
                }

            self._send_json(HTTPStatus.OK, response_payload)
        except ParserInputError as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": (
                        "문서 파싱 중 내부 오류가 발생했다(문서 본문 보호를 위해 상세 원문 생략)."
                    )
                },
            )


class LocalParserUiServer:
    """컨텍스트 매니저로 수명 주기를 관리하는 Auto Parser 로컬 웹 UI 서버."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        allow_non_loopback: bool = False,
    ) -> None:
        if not allow_non_loopback and host not in _LOOPBACK_HOSTS:
            raise ParserInputError(
                f"로컬 웹 UI는 보안상 루프백(127.0.0.1/localhost) 바인딩만 기본 허용한다: '{host}'"
            )
        self._httpd = _ParserUiHttpServer((host, port), _ParserUiRequestHandler)
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return str(self._httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start_background(self) -> str:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
        )
        self._thread.start()
        return self.url

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def __enter__(self) -> LocalParserUiServer:
        self.start_background()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def create_ui_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_non_loopback: bool = False,
) -> LocalParserUiServer:
    """백그라운드 테스트 또는 인터랙티브 실행용 `LocalParserUiServer` 인스턴스를 생성한다."""
    return LocalParserUiServer(host=host, port=port, allow_non_loopback=allow_non_loopback)


def serve_ui(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """`autoetl ui` CLI용 포그라운드 로컬 웹 UI 서버를 기동한다."""
    srv = LocalParserUiServer(host=host, port=port)
    print(f"Auto Parser 로컬 웹 UI 실행 중: {srv.url} (종료: Ctrl+C)")
    if open_browser:
        webbrowser.open(srv.url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n로컬 웹 UI 서버를 종료합니다.")
    finally:
        srv.close()


__all__ = [
    "LocalParserUiServer",
    "create_ui_server",
    "serve_ui",
]
