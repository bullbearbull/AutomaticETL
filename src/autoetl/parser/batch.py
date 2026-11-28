"""폴더 단위 다중 문서 일괄 파싱(`batch`)·오류 격리·SHA-256 재개·통합 청크/요약 리포트 엔진."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import Field

from autoetl.contracts.common import ContractModel
from autoetl.parser.adapters import (
    PageLayoutDetector,
    PageRenderer,
    ParseOptions,
    ParserInputError,
    VisionParserAdapter,
)
from autoetl.parser.chunker import ChunkOptions
from autoetl.parser.exporters import is_bundle_tables_workbook, render_routing_report_markdown
from autoetl.parser.pipeline import (
    inspect_document,
    parse_and_export,
    supported_extensions,
)


class BatchItemResult(ContractModel):
    """폴더 배치 내 개별 문서 1건의 처리 결과."""

    relative_path: str
    format: str
    status: Literal["ok", "skipped_resume", "failed"]
    sha256: str = ""
    file_size_bytes: int = 0
    out_subdir: str | None = None
    page_count: int = 0
    table_count: int = 0
    visual_region_count: int = 0
    chunk_count: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None


class BatchManifest(ContractModel):
    """폴더 배치 전체 실행 결과 및 통합 산출물 매니페스트."""

    input_dir: str
    out_dir: str
    recursive: bool = True
    resume: bool = False
    plan_only: bool = False
    total_files: int = 0
    succeeded: int = 0
    skipped_resume: int = 0
    failed: int = 0
    total_pages: int = 0
    total_tables: int = 0
    total_visual_regions: int = 0
    total_chunks: int = 0
    elapsed_seconds: float = 0.0
    by_format: dict[str, int] = Field(default_factory=dict)
    all_chunks_jsonl: str | None = None
    items: list[BatchItemResult] = Field(default_factory=list)


def _file_sha256(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            buf = fh.read(65536)
            if not buf:
                break
            h.update(buf)
            size += len(buf)
    return h.hexdigest(), size


def _is_temp_office_file(name: str) -> bool:
    return name.startswith(("~$", ".~lock.", "."))


def discover_batch_files(
    input_dir: Path | str,
    *,
    recursive: bool = True,
    include_extensions: tuple[str, ...] | None = None,
) -> list[Path]:
    """입력 폴더에서 지원 확장자 문서 파일 목록을 결정론적 정렬로 탐색한다."""
    root = Path(input_dir)
    if not root.is_dir():
        raise ParserInputError(f"배치 입력 폴더가 존재하지 않는다: {root}")

    supported = set(supported_extensions())
    if include_extensions:
        wanted = {
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in include_extensions
            if ext.strip()
        }
        allowed = supported & wanted
    else:
        allowed = supported

    iterator = root.rglob("*") if recursive else root.glob("*")
    found: list[Path] = []
    for p in iterator:
        if not p.is_file():
            continue
        if _is_temp_office_file(p.name):
            continue
        # 이전 실행이 내보낸 번들 속 tables.xlsx는 입력 문서가 아니다.
        if is_bundle_tables_workbook(p):
            continue
        # 숨김 디렉터리 내부 제외
        rel_parts = p.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts[:-1]):
            continue
        if p.suffix.lower() in allowed:
            found.append(p)
    found.sort(key=lambda p: p.relative_to(root).as_posix().lower())
    return found


def _doc_bundle_dir(out_dir: Path, rel_path: Path) -> Path:
    """동일 폴더의 `a.pdf`와 `a.docx`가 충돌하지 않도록 확장자 접미사를 붙여 출력 경로를 만든다."""
    ext_tag = rel_path.suffix.lstrip(".").lower() or "doc"
    leaf = f"{rel_path.stem}__{ext_tag}"
    return out_dir / "documents" / rel_path.parent / leaf


def _try_resume_cached_item(
    doc_out: Path,
    *,
    rel_posix: str,
    fmt: str,
    file_sha: str,
    file_size: int,
    out_dir: Path,
    include_chunks: bool,
    plan_only: bool,
) -> BatchItemResult | None:
    """기존 출력 번들의 `sha256`이 입력 파일과 일치하면 재파싱을 건너뛴다."""
    meta_file = doc_out / ("routing_report.json" if plan_only else "parsed.json")
    if not meta_file.is_file():
        return None
    if not plan_only and include_chunks and not (doc_out / "chunks.jsonl").is_file():
        return None
    try:
        payload = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    doc_meta = payload.get("document")
    if not isinstance(doc_meta, dict) or doc_meta.get("sha256") != file_sha:
        return None

    if plan_only:
        summary = payload.get("summary") or {}
        pages = int(summary.get("total_pages", 0) or 0)
        tables = int(summary.get("table_count", 0) or 0)
        visuals = int(summary.get("visual_region_count", 0) or 0)
        chunks_cnt = 0
    else:
        pages = len(payload.get("pages") or [])
        tables = len(payload.get("tables") or [])
        visuals = len(payload.get("visual_regions") or [])
        chunks_file = doc_out / "chunks.jsonl"
        if include_chunks and chunks_file.is_file():
            chunks_cnt = sum(
                1 for ln in chunks_file.read_text(encoding="utf-8").splitlines() if ln.strip()
            )
        else:
            chunks_cnt = 0

    return BatchItemResult(
        relative_path=rel_posix,
        format=fmt,
        status="skipped_resume",
        sha256=file_sha,
        file_size_bytes=file_size,
        out_subdir=doc_out.relative_to(out_dir).as_posix(),
        page_count=pages,
        table_count=tables,
        visual_region_count=visuals,
        chunk_count=chunks_cnt,
        elapsed_seconds=0.0,
    )


def render_batch_summary_markdown(manifest: BatchManifest) -> str:
    """`BatchManifest`를 사람이 읽기 쉬운 Markdown 요약 리포트로 렌더링한다."""
    lines: list[str] = [
        "# Auto Parser 일괄 처리 요약 리포트",
        "",
        f"- 입력 폴더: `{manifest.input_dir}`",
        f"- 출력 폴더: `{manifest.out_dir}`",
        f"- 모드: {'구조 진단(--plan-only)' if manifest.plan_only else '전체 파싱'}"
        f" · 하위 폴더 포함: `{'예' if manifest.recursive else '아니오'}`"
        f" · 재개(--resume): `{'예' if manifest.resume else '아니오'}`",
        f"- 처리 결과: 총 **{manifest.total_files}건** "
        f"(성공 **{manifest.succeeded}**, 재개 스킵 **{manifest.skipped_resume}**, "
        f"실패/차단 **{manifest.failed}**)",
        f"- 누적 추출량: 페이지 **{manifest.total_pages}**, 표 **{manifest.total_tables}**, "
        f"시각 영역 **{manifest.total_visual_regions}**, RAG 청크 **{manifest.total_chunks}**",
        f"- 총 소요 시간: `{manifest.elapsed_seconds:.2f}s`",
    ]
    if manifest.all_chunks_jsonl:
        lines.append(f"- 통합 RAG 청크 JSONL: `{manifest.all_chunks_jsonl}`")

    if manifest.by_format:
        lines.extend(["", "## 1. 형식별 분포", "", "| 형식 | 파일 수 |", "| --- | ---: |"])
        for fmt, cnt in sorted(manifest.by_format.items()):
            lines.append(f"| `{fmt}` | {cnt} |")

    lines.extend(
        [
            "",
            "## 2. 파일별 처리 결과",
            "",
            "| 파일 | 형식 | 상태 | 페이지 | 표 | 시각 | 청크 | 소요(s) | 비고 |",
            "| --- | --- | :-: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for it in manifest.items:
        note = (it.error or it.out_subdir or "").replace("|", "\\|")
        lines.append(
            f"| `{it.relative_path}` | `{it.format}` | `{it.status}` | {it.page_count} | "
            f"{it.table_count} | {it.visual_region_count} | {it.chunk_count} | "
            f"{it.elapsed_seconds:.2f} | {note} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def parse_batch(
    input_dir: Path | str,
    out_dir: Path | str,
    *,
    recursive: bool = True,
    include_extensions: tuple[str, ...] | None = None,
    workers: int = 1,
    resume: bool = False,
    fail_fast: bool = False,
    plan_only: bool = False,
    options: ParseOptions | None = None,
    include_chunks: bool = True,
    chunk_options: ChunkOptions | None = None,
    vision_adapter: VisionParserAdapter | None = None,
    page_renderer: PageRenderer | None = None,
    layout_detector: PageLayoutDetector | None = None,
) -> BatchManifest:
    """폴더 내 모든 지원 문서를 일괄 파싱하고 오류 격리·재개·통합 청크·요약 리포트를 생성한다."""
    src_root = Path(input_dir)
    dst_root = Path(out_dir)
    files = discover_batch_files(
        src_root,
        recursive=recursive,
        include_extensions=include_extensions,
    )
    dst_root.mkdir(parents=True, exist_ok=True)
    t_batch_start = time.perf_counter()

    def _process_one(path: Path) -> BatchItemResult:
        rel = path.relative_to(src_root)
        rel_posix = rel.as_posix()
        fmt = path.suffix.lstrip(".").lower()
        doc_out = _doc_bundle_dir(dst_root, rel)
        t0 = time.perf_counter()

        try:
            file_sha, file_size = _file_sha256(path)
        except OSError as exc:
            return BatchItemResult(
                relative_path=rel_posix,
                format=fmt,
                status="failed",
                elapsed_seconds=round(time.perf_counter() - t0, 3),
                error=f"파일 읽기 오류: {exc.strerror or type(exc).__name__}",
            )

        if resume:
            cached = _try_resume_cached_item(
                doc_out,
                rel_posix=rel_posix,
                fmt=fmt,
                file_sha=file_sha,
                file_size=file_size,
                out_dir=dst_root,
                include_chunks=include_chunks,
                plan_only=plan_only,
            )
            if cached is not None:
                return cached

        try:
            if plan_only:
                plan = inspect_document(
                    path,
                    options=options,
                    layout_detector=layout_detector,
                    page_renderer=page_renderer,
                )
                doc_out.mkdir(parents=True, exist_ok=True)
                (doc_out / "routing_report.json").write_text(
                    plan.model_dump_json(indent=2) + "\n",
                    encoding="utf-8",
                )
                (doc_out / "routing_report.md").write_text(
                    render_routing_report_markdown(plan),
                    encoding="utf-8",
                )
                return BatchItemResult(
                    relative_path=rel_posix,
                    format=plan.document.format.value,
                    status="ok",
                    sha256=file_sha,
                    file_size_bytes=file_size,
                    out_subdir=doc_out.relative_to(dst_root).as_posix(),
                    page_count=plan.summary.total_pages,
                    table_count=plan.summary.table_count,
                    visual_region_count=plan.summary.visual_region_count,
                    chunk_count=0,
                    elapsed_seconds=round(time.perf_counter() - t0, 3),
                )

            doc, manifest = parse_and_export(
                path,
                doc_out,
                options=options,
                vision_adapter=vision_adapter,
                page_renderer=page_renderer,
                layout_detector=layout_detector,
                include_chunks=include_chunks,
                chunk_options=chunk_options,
            )
            return BatchItemResult(
                relative_path=rel_posix,
                format=doc.document.format.value,
                status="ok",
                sha256=file_sha,
                file_size_bytes=file_size,
                out_subdir=doc_out.relative_to(dst_root).as_posix(),
                page_count=len(doc.pages),
                table_count=len(doc.tables),
                visual_region_count=len(doc.visual_regions),
                chunk_count=manifest.chunk_count,
                elapsed_seconds=round(time.perf_counter() - t0, 3),
            )
        except ParserInputError as exc:
            return BatchItemResult(
                relative_path=rel_posix,
                format=fmt,
                status="failed",
                sha256=file_sha,
                file_size_bytes=file_size,
                elapsed_seconds=round(time.perf_counter() - t0, 3),
                error=str(exc),
            )
        except Exception:
            # 문서 본문 보호를 위해 미처리 내부 예외 원문은 기록하지 않는다.
            return BatchItemResult(
                relative_path=rel_posix,
                format=fmt,
                status="failed",
                sha256=file_sha,
                file_size_bytes=file_size,
                elapsed_seconds=round(time.perf_counter() - t0, 3),
                error="내부 파싱 오류(문서 본문 보호를 위해 상세 원문 생략)",
            )

    results: list[BatchItemResult] = []
    eff_workers = max(1, workers)
    if eff_workers == 1 or fail_fast or len(files) <= 1:
        for fpath in files:
            res = _process_one(fpath)
            results.append(res)
            if fail_fast and res.status == "failed":
                break
    else:
        with ThreadPoolExecutor(max_workers=eff_workers) as pool:
            results = list(pool.map(_process_one, files))

    # 성공·재개된 개별 문서의 chunks.jsonl을 단일 all_chunks.jsonl로 병합
    all_chunks_rel: str | None = None
    if not plan_only and include_chunks:
        merged_lines: list[str] = []
        for it in results:
            if it.status in ("ok", "skipped_resume") and it.out_subdir:
                cfile = dst_root / it.out_subdir / "chunks.jsonl"
                if cfile.is_file():
                    for ln in cfile.read_text(encoding="utf-8").splitlines():
                        if ln.strip():
                            merged_lines.append(ln)
        all_chunks_path = dst_root / "all_chunks.jsonl"
        all_chunks_path.write_text(
            ("\n".join(merged_lines) + "\n") if merged_lines else "",
            encoding="utf-8",
        )
        all_chunks_rel = all_chunks_path.relative_to(dst_root).as_posix()

    by_fmt: dict[str, int] = {}
    for it in results:
        by_fmt[it.format] = by_fmt.get(it.format, 0) + 1

    manifest = BatchManifest(
        input_dir=src_root.as_posix(),
        out_dir=dst_root.as_posix(),
        recursive=recursive,
        resume=resume,
        plan_only=plan_only,
        total_files=len(files),
        succeeded=sum(1 for it in results if it.status == "ok"),
        skipped_resume=sum(1 for it in results if it.status == "skipped_resume"),
        failed=sum(1 for it in results if it.status == "failed"),
        total_pages=sum(it.page_count for it in results),
        total_tables=sum(it.table_count for it in results),
        total_visual_regions=sum(it.visual_region_count for it in results),
        total_chunks=sum(it.chunk_count for it in results),
        elapsed_seconds=round(time.perf_counter() - t_batch_start, 3),
        by_format=by_fmt,
        all_chunks_jsonl=all_chunks_rel,
        items=results,
    )

    (dst_root / "batch_manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    (dst_root / "batch_summary.md").write_text(
        render_batch_summary_markdown(manifest),
        encoding="utf-8",
    )
    return manifest


__all__ = [
    "BatchItemResult",
    "BatchManifest",
    "discover_batch_files",
    "parse_batch",
    "render_batch_summary_markdown",
]
