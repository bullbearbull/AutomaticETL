"""다중 파일·다중 시트 분석 컨텍스트(`MultiTableContext`, Milestone E1).

여러 CSV/TSV 파일, 멀티 시트 XLSX 워크북, 또는 메모리 추출 표(`NamedTableInput`)를
단일 잠금 DuckDB 세션의 독립 스키마(`t0`, `t1`, ...)에 적재한다.

보안·설계 원칙:
- 각 논리 테이블은 자체 스키마(`t0`, `t1`, ...) 안에 `raw`, `s1_sample`, `normalized`를
  두므로 기존 단일 테이블 단계(S0~S5)를 수정 없이 그대로 재사용하면서,
  다중 테이블 교차 분석(E3)에서는 `t0.normalized` ↔ `t1.normalized` 조인을
  단일 DuckDB 연결 메모리 안에서 직렬화 없이 수행한다.
- 원본 파일명·시트명·헤더 문자열은 SQL 식별자로 절대 쓰지 않는다. SQL에는
  코드가 생성한 내부 식별자(`t0`, `c0` 등)만 사용한다.
- 모든 원본 적재가 끝난 직후 `SET enable_external_access = false`로 연결을 잠근다.
"""

from __future__ import annotations

import csv
import hashlib
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from autoetl.contracts import (
    IngestFormat,
    IngestManifest,
    SourceColumn,
    SourceFile,
    TableProfileBundle,
    TableSourceInfo,
    TableSourceKind,
)
from autoetl.core.context import (
    INTERNAL_ID_OF_POSITION,
    RAW_TABLE,
    AnalysisContext,
    ContextColumn,
)
from autoetl.core.errors import InputError
from autoetl.core.stages import (
    s00_ingest,
    s01_sampling,
    s02_profile,
    s03_types,
    s04_stats,
)
from autoetl.core.stages.s00_ingest import (
    SourceSpec,
    deduplicate_source_names,
    probe_source,
)

SUPPORTED_TABLE_EXTENSIONS = frozenset({".csv", ".tsv", ".xlsx"})
_INTERNAL_SCHEMA_RE = re.compile(r"^t\d+$")
_IGNORED_PREFIXES = ("~$", ".~lock.")


@dataclass(frozen=True)
class NamedTableInput:
    """메모리 또는 Auto Parser 추출 표에서 직접 적재하는 테이블 입력."""

    name: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    source_path: str = "<memory>"
    sheet_name: str | None = None
    source_kind: TableSourceKind = TableSourceKind.EXTRACTED_TABLE


def sanitize_table_names(raw_names: Sequence[str]) -> list[str]:
    """원본 파일명·시트명을 PostgreSQL 안전 식별자(`^[a-z][a-z0-9_]*$`)로 유일화한다."""
    safe: list[str] = []
    taken: set[str] = set()
    for index, raw in enumerate(raw_names):
        cleaned = "".join(ch if ch.isascii() and ch.isalnum() else "_" for ch in raw.lower())
        parts = [part for part in cleaned.split("_") if part]
        base = "_".join(parts)
        if not base or not base[0].isalpha():
            base = f"table_{index + 1}"
        # PostgreSQL NAMEDATALEN(63) 여유 확보
        base = base[:54].rstrip("_") or f"table_{index + 1}"
        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = f"{base}_{suffix}"
            suffix += 1
        taken.add(candidate)
        safe.append(candidate)
    return safe


def _is_ignored_file(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return True
    return any(name.startswith(prefix) for prefix in _IGNORED_PREFIXES)


def discover_table_sources(
    inputs: Path | str | Sequence[Path | str],
    *,
    recursive: bool = True,
) -> list[Path]:
    """파일·폴더 목록에서 지원 테이블 파일(`.csv`, `.tsv`, `.xlsx`)을 결정론적 순서로 수집한다."""
    raw_list: list[Path] = (
        [Path(inputs)] if isinstance(inputs, (Path, str)) else [Path(p) for p in inputs]
    )
    if not raw_list:
        raise InputError("분석할 테이블 입력 파일 또는 폴더가 비어 있다.")

    found: list[Path] = []
    seen: set[Path] = set()

    for item in raw_list:
        if not item.exists():
            raise InputError(f"입력 파일 또는 폴더가 없다: {item}")
        if item.is_file():
            ext = item.suffix.lower()
            if ext not in SUPPORTED_TABLE_EXTENSIONS:
                raise InputError(
                    f"지원하지 않는 테이블 형식이다: '{ext or '(없음)'}' "
                    f"(지원: {', '.join(sorted(SUPPORTED_TABLE_EXTENSIONS))})"
                )
            resolved = item.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(item)
            continue
        if item.is_dir():
            iterator = item.rglob("*") if recursive else item.glob("*")
            dir_files = sorted(
                (
                    p
                    for p in iterator
                    if p.is_file()
                    and not _is_ignored_file(p)
                    and p.suffix.lower() in SUPPORTED_TABLE_EXTENSIONS
                ),
                key=lambda p: p.as_posix().lower(),
            )
            for p in dir_files:
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(p)

    if not found:
        raise InputError("입력 경로에서 처리할 CSV·TSV·XLSX 파일을 찾지 못했다.")
    return found


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TableSlotContext(AnalysisContext):
    """단일 논리 테이블용 스키마 범위 `AnalysisContext` 어댑터.

    `MultiTableContext`가 소유한 DuckDB 연결과 임시 디렉터리를 공유하며,
    `close()`는 아무 것도 하지 않아 개별 테이블 분석 후에도 교차 쿼리가 유지된다.
    """

    def __init__(
        self,
        *,
        table_info: TableSourceInfo,
        connection: duckdb.DuckDBPyConnection,
        input_path: Path,
        spec: SourceSpec,
        source_sha256: str | None = None,
    ) -> None:
        self.table_info = table_info
        self.source_sha256 = source_sha256
        self.input_path = input_path
        self.source_name = table_info.display_name
        self.spec = spec
        self.header = list(spec.header)
        self.row_count = spec.row_count
        self.size_bytes = spec.size_bytes
        self.columns = tuple(
            ContextColumn(
                position=position,
                original_name=name,
                column_id=INTERNAL_ID_OF_POSITION.format(position=position),
            )
            for position, name in enumerate(self.header)
        )
        self.normalized_view: str | None = None
        self.stage_artifacts: dict[str, object] = {}
        self.connection = connection
        self._temp_dir = Path()

    def close(self) -> None:
        """연결 수명은 부모 `MultiTableContext`가 관리하므로 닫지 않는다."""
        return


@dataclass
class _StagedTable:
    raw_name: str
    display_name: str
    source_path: str
    source_kind: TableSourceKind
    sheet_name: str | None
    input_path: Path
    spec: SourceSpec
    source_sha256: str | None
    extra_temp_file: Path | None = None


def _write_rows_to_temp_csv(
    temp_dir: Path,
    filename: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> tuple[Path, int]:
    """정규화된 헤더·행 목록을 임시 UTF-8 CSV로 저장하고 바이트 크기를 반환한다."""
    n_cols = len(headers)
    clean_headers = [
        h.replace("\r", " ").replace("\n", " ").strip() or f"col_{i + 1}"
        for i, h in enumerate(headers)
    ]
    out_path = temp_dir / filename
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=",")
        writer.writerow(clean_headers)
        for row in rows:
            padded = [
                (row[i] if i < len(row) else "").replace("\r", " ").replace("\n", " ")
                for i in range(n_cols)
            ]
            writer.writerow(padded)
    return out_path, out_path.stat().st_size


def _stage_xlsx_tables(
    xlsx_path: Path,
    *,
    temp_dir: Path,
    single_file_mode: bool,
    warnings: list[str],
    counter_start: int,
) -> list[_StagedTable]:
    """무의존성 `XlsxDocumentAdapter`로 `.xlsx` 시트별 표를 추출해 스테이징한다."""
    from autoetl.parser.adapters import ParseOptions, ParserInputError
    from autoetl.parser.xlsx.adapter import XlsxDocumentAdapter

    adapter = XlsxDocumentAdapter()
    try:
        doc = adapter.parse(xlsx_path, options=ParseOptions(extract_crops=False))
    except ParserInputError as exc:
        raise InputError(f"XLSX 파일을 읽지 못했다({xlsx_path.name}): {exc}") from exc

    file_sha = doc.document.sha256
    staged: list[_StagedTable] = []
    idx = counter_start

    for page in doc.pages:
        sheet_title = next(
            (b.text.strip() for b in page.blocks if b.text.strip()),
            f"sheet_{page.page}",
        )
        non_empty_tables = [t for t in page.tables if t.row_count > 0 and t.col_count > 0]
        if not non_empty_tables:
            warnings.append(
                f"{xlsx_path.name}::{sheet_title} 시트에 데이터 행이 있는 표가 없어 건너뛰었다."
            )
            continue

        for t_idx, tbl in enumerate(non_empty_tables):
            n_cols = tbl.col_count
            headers = list(tbl.headers[:n_cols])
            if len(headers) < n_cols:
                headers.extend(f"col_{i + 1}" for i in range(len(headers), n_cols))
            suffix_label = f"_{t_idx + 1}" if len(non_empty_tables) > 1 else ""
            raw_table_name = (
                f"{sheet_title}{suffix_label}"
                if single_file_mode
                else f"{xlsx_path.stem}_{sheet_title}{suffix_label}"
            )
            display = f"{xlsx_path.name}::{sheet_title}{suffix_label}"
            csv_path, size_bytes = _write_rows_to_temp_csv(
                temp_dir,
                f"staged_xlsx_{idx}.csv",
                headers,
                tbl.rows,
            )
            idx += 1
            spec = SourceSpec(
                original_path=xlsx_path,
                load_path=csv_path,
                converted=True,
                encoding="utf-8",
                delimiter=",",
                header_row_count=1,
                skip_lines=1,
                header=[
                    h.replace("\r", " ").replace("\n", " ").strip() or f"col_{i + 1}"
                    for i, h in enumerate(headers)
                ],
                row_count=len(tbl.rows),
                size_bytes=size_bytes,
                applied_normalizations=["xlsx_sheet_extracted"],
                warnings=[],
            )
            staged.append(
                _StagedTable(
                    raw_name=raw_table_name,
                    display_name=display,
                    source_path=xlsx_path.name,
                    source_kind=TableSourceKind.XLSX_SHEET,
                    sheet_name=sheet_title,
                    input_path=xlsx_path,
                    spec=spec,
                    source_sha256=file_sha,
                    extra_temp_file=csv_path,
                )
            )
    return staged


class MultiTableContext:
    """여러 CSV/TSV/XLSX/추출 표를 단일 잠금 DuckDB 세션에 적재·관리하는 컨텍스트."""

    def __init__(
        self,
        inputs: Path | str | Sequence[Path | str] | None = None,
        *,
        extra_tables: Sequence[NamedTableInput] = (),
        recursive: bool = True,
        memory_limit: str | None = None,
        threads: int | None = None,
    ) -> None:
        self._temp_dir = Path(tempfile.mkdtemp(prefix="autoetl-multi-duckdb-"))
        self._temp_files: list[Path] = []
        self.warnings: list[str] = []
        self.slots: list[TableSlotContext] = []
        self._slots_by_id: dict[str, TableSlotContext] = {}
        self._slots_by_name: dict[str, TableSlotContext] = {}

        config: dict[str, Any] = {
            "temp_directory": str(self._temp_dir),
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        }
        if memory_limit is not None:
            config["memory_limit"] = memory_limit
        if threads is not None:
            config["threads"] = threads

        self.connection: duckdb.DuckDBPyConnection | None = None
        try:
            file_paths: list[Path] = []
            if inputs is not None:
                file_paths = discover_table_sources(inputs, recursive=recursive)
            elif not extra_tables:
                raise InputError("분석할 파일 경로 또는 메모리 테이블 입력이 하나 이상 필요하다.")

            staged: list[_StagedTable] = []
            single_xlsx_mode = (
                len(file_paths) == 1
                and file_paths[0].suffix.lower() == ".xlsx"
                and not extra_tables
            )
            xlsx_counter = 0

            for path in file_paths:
                ext = path.suffix.lower()
                if ext == ".xlsx":
                    xlsx_tables = _stage_xlsx_tables(
                        path,
                        temp_dir=self._temp_dir,
                        single_file_mode=single_xlsx_mode,
                        warnings=self.warnings,
                        counter_start=xlsx_counter,
                    )
                    xlsx_counter += len(xlsx_tables)
                    for item in xlsx_tables:
                        if item.extra_temp_file is not None:
                            self._temp_files.append(item.extra_temp_file)
                    staged.extend(xlsx_tables)
                else:
                    spec = probe_source(path)
                    AnalysisContext._validate_spec(spec, path)
                    if spec.converted:
                        self._temp_files.append(spec.load_path)
                    kind = TableSourceKind.TSV if ext == ".tsv" else TableSourceKind.CSV
                    staged.append(
                        _StagedTable(
                            raw_name=path.stem,
                            display_name=path.name,
                            source_path=path.name,
                            source_kind=kind,
                            sheet_name=None,
                            input_path=path,
                            spec=spec,
                            source_sha256=_sha256_file(path),
                        )
                    )

            for mem_idx, mem in enumerate(extra_tables):
                if not mem.headers:
                    self.warnings.append(f"'{mem.name}' 테이블에 헤더가 없어 건너뛰었다.")
                    continue
                if not mem.rows:
                    self.warnings.append(f"'{mem.name}' 테이블에 데이터 행이 없어 건너뛰었다.")
                    continue
                csv_path, size_bytes = _write_rows_to_temp_csv(
                    self._temp_dir,
                    f"staged_mem_{mem_idx}.csv",
                    mem.headers,
                    mem.rows,
                )
                self._temp_files.append(csv_path)
                clean_headers = [
                    h.replace("\r", " ").replace("\n", " ").strip() or f"col_{i + 1}"
                    for i, h in enumerate(mem.headers)
                ]
                spec = SourceSpec(
                    original_path=Path(mem.source_path),
                    load_path=csv_path,
                    converted=True,
                    encoding="utf-8",
                    delimiter=",",
                    header_row_count=1,
                    skip_lines=1,
                    header=clean_headers,
                    row_count=len(mem.rows),
                    size_bytes=size_bytes,
                    applied_normalizations=[],
                    warnings=[],
                )
                staged.append(
                    _StagedTable(
                        raw_name=mem.name,
                        display_name=mem.name,
                        source_path=mem.source_path,
                        source_kind=mem.source_kind,
                        sheet_name=mem.sheet_name,
                        input_path=Path(mem.source_path),
                        spec=spec,
                        source_sha256=None,
                        extra_temp_file=csv_path,
                    )
                )

            if not staged:
                raise InputError("적재할 유효한 테이블(데이터 행 ≥ 1)이 하나도 없다.")

            safe_names = sanitize_table_names([item.raw_name for item in staged])
            self.connection = duckdb.connect(database=":memory:", config=config)

            for index, (item, safe_name) in enumerate(zip(staged, safe_names, strict=True)):
                schema_id = f"t{index}"
                self.connection.execute(f"CREATE SCHEMA {schema_id}")
                self.connection.execute(f"SET schema = '{schema_id}'")

                info = TableSourceInfo(
                    table_id=schema_id,
                    table_name=safe_name,
                    display_name=item.display_name,
                    source_path=item.source_path,
                    source_kind=item.source_kind,
                    sheet_name=item.sheet_name,
                    row_count=item.spec.row_count,
                    column_count=len(item.spec.header),
                )
                slot = TableSlotContext(
                    table_info=info,
                    connection=self.connection,
                    input_path=item.input_path,
                    spec=item.spec,
                    source_sha256=item.source_sha256,
                )
                self._load_slot_into_current_schema(slot)
                self.slots.append(slot)
                self._slots_by_id[schema_id] = slot
                self._slots_by_name[safe_name] = slot

            # 모든 원본 적재가 끝났으므로 외부 파일 접근 차단(심층 방어)
            self.connection.execute("SET enable_external_access = false")
        except BaseException:
            self.close()
            raise

    def _load_slot_into_current_schema(self, slot: TableSlotContext) -> None:
        assert self.connection is not None
        columns_spec = ", ".join(f"'{col.column_id}': 'VARCHAR'" for col in slot.columns)
        try:
            self.connection.execute(
                f"CREATE TABLE {RAW_TABLE} AS SELECT * FROM read_csv(?, "
                f"header=false, skip={slot.spec.skip_lines}, "
                f"columns={{{columns_spec}}}, "
                f"allow_quoted_nulls=false, delim='{slot.spec.delimiter}')",
                [str(slot.spec.load_path)],
            )
            loaded = self.connection.execute(f"SELECT COUNT(*) FROM {RAW_TABLE}").fetchone()
            if loaded is None or loaded[0] != slot.spec.row_count:
                raise InputError(
                    f"적재한 행 수가 감지한 행 수와 다르다({slot.table_info.display_name})."
                )
        except duckdb.Error as exc:
            raise InputError(
                f"테이블을 DuckDB에 적재하지 못했다({slot.table_info.display_name}): "
                f"{type(exc).__name__}"
            ) from exc

    def activate(self, table_id: str) -> TableSlotContext:
        """지정한 내부 스키마(`t0`, `t1`, ...)를 현재 DuckDB 기본 스키마로 전환한다."""
        if not _INTERNAL_SCHEMA_RE.fullmatch(table_id) or table_id not in self._slots_by_id:
            raise InputError(f"알 수 없는 내부 테이블 ID다: {table_id}")
        assert self.connection is not None
        self.connection.execute(f"SET schema = '{table_id}'")
        return self._slots_by_id[table_id]

    def get_by_id(self, table_id: str) -> TableSlotContext:
        return self._slots_by_id[table_id]

    def get_by_name(self, table_name: str) -> TableSlotContext:
        return self._slots_by_name[table_name]

    @property
    def table_infos(self) -> list[TableSourceInfo]:
        return [slot.table_info for slot in self.slots]

    def close(self) -> None:
        """DuckDB 연결과 모든 임시 파일·폴더를 정리한다. 여러 번 호출해도 안전하다."""
        conn = getattr(self, "connection", None)
        if conn is not None:
            conn.close()
            self.connection = None
        for tf in getattr(self, "_temp_files", ()):
            tf.unlink(missing_ok=True)
        temp_dir = getattr(self, "_temp_dir", None)
        if temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    def __enter__(self) -> MultiTableContext:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _build_ingest_manifest(slot: TableSlotContext) -> IngestManifest:
    """`TableSlotContext`용 S0 `IngestManifest`(CSV/TSV/XLSX 포맷·시트명 포함)를 생성한다."""
    kind = slot.table_info.source_kind
    if kind is TableSourceKind.XLSX_SHEET:
        fmt = IngestFormat.XLSX
    elif kind is TableSourceKind.TSV:
        fmt = IngestFormat.TSV
    else:
        fmt = IngestFormat.CSV

    if fmt is IngestFormat.CSV and slot.table_info.sheet_name is None:
        manifest = s00_ingest.run(slot, slot.spec)
        if slot.source_sha256 is not None and manifest.files:
            updated_file = manifest.files[0].model_copy(update={"sha256": slot.source_sha256})
            return manifest.model_copy(update={"files": [updated_file]})
        return manifest

    sources = deduplicate_source_names(slot.spec.header)
    return IngestManifest(
        files=[
            SourceFile(
                path=slot.table_info.source_path,
                format=fmt,
                encoding=slot.spec.encoding,
                size_bytes=slot.spec.size_bytes,
                sha256=slot.source_sha256,
                sheet=slot.table_info.sheet_name,
            )
        ],
        columns=[SourceColumn(name=name, index=i) for i, name in enumerate(sources)],
        row_count=slot.spec.row_count,
        delimiter=slot.spec.delimiter,
        header_row_count=slot.spec.header_row_count,
        applied_normalizations=slot.spec.applied_normalizations,
        warnings=slot.spec.warnings,
    )


def profile_all_tables(mctx: MultiTableContext) -> list[TableProfileBundle]:
    """`MultiTableContext`의 모든 테이블에 S0~S4 및 E2 정밀 칼럼 분류를 실행한다."""
    from autoetl.core.column_classifier import classify_table_columns

    bundles: list[TableProfileBundle] = []
    for slot in mctx.slots:
        mctx.activate(slot.table_info.table_id)
        ingest = _build_ingest_manifest(slot)
        slot.stage_artifacts["s00_ingest"] = ingest
        sampling = s01_sampling.run(slot)
        slot.stage_artifacts["s01_sampling"] = sampling
        profiles = s02_profile.run(slot, ingest, sampling)
        slot.stage_artifacts["s02_profile"] = profiles
        types = s03_types.run(slot, ingest, profiles)
        slot.stage_artifacts["s03_types"] = types
        stats = s04_stats.run(slot, ingest, types)
        slot.stage_artifacts["s04_stats"] = stats
        classifications = classify_table_columns(slot, ingest, profiles, types, stats)
        slot.stage_artifacts["e2_classifications"] = classifications
        bundles.append(
            TableProfileBundle(
                source=slot.table_info,
                ingest=ingest,
                sampling=sampling,
                profiles=profiles,
                types=types,
                stats=stats,
                classifications=classifications,
                warnings=list(ingest.warnings),
            )
        )
    return bundles


__all__ = [
    "SUPPORTED_TABLE_EXTENSIONS",
    "MultiTableContext",
    "NamedTableInput",
    "TableSlotContext",
    "discover_table_sources",
    "profile_all_tables",
    "sanitize_table_names",
]
