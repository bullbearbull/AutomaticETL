"""autoetl CLI v0.

    autoetl analyze <file.csv> --no-llm [--out report.json]

v0은 LLM 없는 경로(--no-llm)만 지원한다. LLM 하네스 연동은 P2 이후.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autoetl import __version__
from autoetl.contracts import Purpose
from autoetl.core.pipeline import InputError


def _targets_same_file(out: Path, source: Path) -> bool:
    """--out이 입력 파일을 가리키는지 검사한다.

    resolve() 비교는 심볼릭 링크와 같은 경로를 잡지만 하드 링크·대소문자 무시
    파일시스템(macOS 기본 등)의 다른 표기는 못 잡는다. samefile(아이노드 비교)을
    함께 써서 둘 다 막는다. samefile은 양쪽 파일이 있을 때만 쓸 수 있다.
    """
    if out.resolve() == source.resolve():
        return True
    return out.exists() and source.exists() and out.samefile(source)


def _run_analyze(args: argparse.Namespace) -> int:
    from autoetl.core.load_planner import export_multi_table_bundle, render_analysis_report
    from autoetl.core.multi_analyze import analyze_tables

    inputs: list[Path] = list(args.files)
    for src in inputs:
        if not src.exists():
            print(f"입력 파일 또는 폴더가 없다: {src}", file=sys.stderr)
            return 2
        if args.out is not None and _targets_same_file(args.out, src):
            print("--out이 입력 경로와 같다. 원본을 덮어쓰지 않는다.", file=sys.stderr)
            return 2

    try:
        bundle = analyze_tables(
            sources=inputs,
            purpose=Purpose(args.purpose),
            decompose_strings=not args.no_decompose_strings,
            recursive=not args.no_recursive,
        )
    except UnicodeDecodeError:
        print(
            "입력 파일 인코딩을 해독할 수 없다. UTF-8/CP949/EUC-KR CSV·TSV·XLSX를 확인하라.",
            file=sys.stderr,
        )
        return 1
    except InputError as exc:
        print(f"입력을 처리할 수 없다: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "분석 파이프라인이 내부 오류로 끝났다(데이터 보호를 위해 상세 원문은 표시하지 않는다). "
            "실행 명령·autoetl 버전·파일 형식·컬럼 구조 정보만 확인해 달라.",
            file=sys.stderr,
        )
        return 3

    lp = bundle.load_plan
    if args.out is not None:
        out_path: Path = args.out
        if out_path.suffix.lower() == ".json":
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
            print(
                f"분석 번들 JSON 저장 완료: {out_path} "
                f"(테이블 {len(bundle.tables)}개, FK {len(bundle.relations.foreign_keys)}건, "
                f"후보 {lp.selected_candidate_id})"
            )
        else:
            written = export_multi_table_bundle(bundle, out_path)
            slot_fk_cnt = sum(
                1 for fk in bundle.relations.foreign_keys if fk.child_slot_name is not None
            )
            print(
                f"다중 테이블 분석 완료: 테이블 {len(bundle.tables)}개, "
                f"FK {len(bundle.relations.foreign_keys)}건(숨은 슬롯 FK {slot_fk_cnt}건), "
                f"선택 후보 {lp.selected_candidate_id} ({bundle.purpose.value})"
            )
            for label, fpath in written.items():
                print(f"- {label}: {fpath}")
        return 0

    if args.format == "json":
        print(bundle.model_dump_json(indent=2))
    elif args.format == "ddl":
        ddl_out = lp.ddl_sql.rstrip()
        if lp.index_ddl_sql.strip():
            ddl_out += "\n\n-- Recommended Indexes\n" + lp.index_ddl_sql.rstrip()
        if lp.comment_ddl_sql.strip():
            ddl_out += "\n\n-- Column Comments\n" + lp.comment_ddl_sql.rstrip()
        print(ddl_out)
    elif args.format == "erd":
        print(lp.erd_mermaid, end="")
    else:
        print(render_analysis_report(bundle), end="")
    return 0


def _run_load(args: argparse.Namespace) -> int:
    import json

    from autoetl.contracts import OrphanPolicy, SchemaOverrideSpec
    from autoetl.core.db_loader import load_tables

    inputs: list[Path] = list(args.files)
    for src in inputs:
        if not src.exists():
            print(f"입력 파일 또는 폴더가 없다: {src}", file=sys.stderr)
            return 2

    try:
        if args.overrides is not None:
            if not args.overrides.is_file():
                print(f"오버라이드 JSON 파일이 없다: {args.overrides}", file=sys.stderr)
                return 2
            raw_ov = json.loads(args.overrides.read_text(encoding="utf-8"))
            spec = SchemaOverrideSpec.model_validate(raw_ov)
        else:
            spec = SchemaOverrideSpec()

        spec = spec.model_copy(
            update={
                "orphan_policy": OrphanPolicy(args.on_error),
                "include_audit_columns": not args.no_audit_columns,
                "include_management_tables": not args.no_management_tables,
            }
        )

        _bundle, result = load_tables(
            sources=inputs,
            db_path=args.db,
            engine=args.engine,
            purpose=Purpose(args.purpose),
            decompose_strings=not args.no_decompose_strings,
            overrides=spec,
            auto_mask_pii=args.mask_pii,
            force_reload=args.force,
            recursive=not args.no_recursive,
            notes=args.notes,
        )
    except InputError as exc:
        print(f"DB 적재를 진행할 수 없다: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "DB 적재 파이프라인이 내부 오류로 끝났다"
            "(데이터 보호를 위해 상세 원문은 표시하지 않는다).",
            file=sys.stderr,
        )
        return 3

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(
            f"DB 적재 완료 [{result.status}]: {result.db_target} ({result.db_engine}) "
            f"| 배치 ID: {result.batch_id} | 시점: {result.loaded_at}"
        )
        print(
            f"- 읽은 행: {result.total_rows_read:,}건 | 저장: {result.total_rows_loaded:,}건 | "
            f"격리: {result.total_rows_quarantined:,}건 ({result.elapsed_seconds:.2f}s)"
        )
        if result.management_tables_created:
            print(f"- 기본 관리 테이블: {', '.join(result.management_tables_created)}")
        for tbl in result.tables:
            skip_lbl = " [중복 파일 건너뜀]" if tbl.skipped_duplicate_file else ""
            drift_lbl = " [스키마 변경 감지]" if tbl.schema_drift_detected else ""
            mask_lbl = (
                f" [개인정보 보호: {', '.join(tbl.masked_columns)}]" if tbl.masked_columns else ""
            )
            print(
                f"  * {tbl.target_table} ({tbl.dataset_kind.value}, ID={tbl.dataset_id}): "
                f"저장 {tbl.rows_loaded:,}건 / 격리 {tbl.rows_quarantined:,}건"
                f"{skip_lbl}{drift_lbl}{mask_lbl}"
            )
        for warn in result.warnings:
            print(f"- 주의: {warn}")
    return 1 if result.status == "failed" else 0


def _run_key(args: argparse.Namespace) -> int:
    from autoetl.security.masking_key import (
        export_masking_key,
        import_masking_key,
        masking_key_path,
        read_masking_key,
    )

    try:
        if args.key_command == "status":
            path = masking_key_path()
            active_key = read_masking_key(path)
            db_key_id = None
            if args.db is not None:
                from autoetl.core.db_loader import masking_key_id_for_database

                db_key_id = masking_key_id_for_database(args.db)

            print(f"마스킹 키 경로 {path}")
            if active_key is None:
                print("마스킹 키 없음")
            else:
                print("마스킹 키 있음")
                print(f"key_id={active_key.key_id}")
                print(f"created_at={active_key.created_at}")
                if active_key.permission_warning:
                    print(f"주의 {active_key.permission_warning}", file=sys.stderr)
            if args.db is not None:
                if db_key_id is None:
                    print("DB 마스킹 키 ID 기록 없음")
                elif active_key is None:
                    print(f"DB key_id={db_key_id} (로컬 키 없음)")
                elif active_key.key_id == db_key_id:
                    print(f"DB key_id={db_key_id} (로컬 키와 일치)")
                else:
                    print(f"DB key_id={db_key_id} (로컬 키와 불일치)")
            return 0

        if args.key_command == "export":
            exported = export_masking_key(args.file)
            print(f"마스킹 키 내보내기 완료 {exported.path}")
            print(
                "주의 이 파일을 가진 사람은 해시로 원래 값을 추측할 수 있습니다. "
                "키 파일을 안전한 장소에 보관하세요.",
                file=sys.stderr,
            )
            if exported.permission_warning:
                print(f"주의 {exported.permission_warning}", file=sys.stderr)
            return 0

        previous_key = read_masking_key() if args.replace else None
        imported, changed = import_masking_key(args.file, replace=args.replace)
        if changed:
            print(f"마스킹 키 가져오기 완료 {imported.path} (key_id={imported.key_id})")
            if previous_key is not None:
                backup = previous_key.path.with_name(f"masking_key.{previous_key.key_id}.bak.json")
                print(f"이전 키 백업 파일 {backup}")
        else:
            print(f"동일한 마스킹 키가 이미 설치되어 있습니다 (key_id={imported.key_id}).")
        return 0
    except InputError as exc:
        print(f"마스킹 키 작업 실패. {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("마스킹 키 작업이 내부 오류로 끝났습니다.", file=sys.stderr)
        return 1


def _run_catalog(args: argparse.Namespace) -> int:
    import json

    from autoetl.core.db_loader import inspect_db_catalog, rollback_batch

    if args.engine in {"sqlite", "duckdb"} and not Path(args.db).is_file():
        print(f"DB 파일이 없다: {args.db}", file=sys.stderr)
        return 2

    try:
        if args.rollback:
            res = rollback_batch(args.db, args.rollback, engine=args.engine)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                restored_total = int(res.get("total_restored", 0))
                restored_suffix = f", {restored_total:,}행 복원" if restored_total else ""
                print(
                    f"배치 롤백 완료: {res['batch_id']} "
                    f"(총 {res['total_deleted']:,}행 삭제{restored_suffix}, 상태=rolled_back)"
                )
                restored_by_tbl = res.get("restored_by_table", {})
                for tbl, cnt in res["deleted_by_table"].items():
                    r_cnt = int(restored_by_tbl.get(tbl, 0))
                    r_part = f" / {r_cnt:,}행 복원" if r_cnt else ""
                    print(f"  * {tbl}: {cnt:,}행 삭제{r_part}")
            return 0

        catalog = inspect_db_catalog(args.db, engine=args.engine, limit=max(1, args.limit))
    except InputError as exc:
        print(f"카탈로그 조회/롤백 실패: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "카탈로그 조회 중 내부 오류가 발생했다(데이터 보호를 위해 상세 원문 생략).",
            file=sys.stderr,
        )
        return 3

    if args.json:
        print(json.dumps(catalog, ensure_ascii=False, indent=2))
    else:
        print(f"DB 카탈로그: {catalog['db_target']} ({catalog['db_engine']})")
        print(f"- 업무 표: {len(catalog['business_tables'])}개")
        for bt in catalog["business_tables"]:
            print(f"  * {bt['table']}: {bt['row_count']:,}행 ({len(bt['columns'])}컬럼)")
        print(f"- 최근 적재 배치: {len(catalog['batches'])}건")
        for b in catalog["batches"][:10]:
            print(
                f"  * {b['batch_id']} [{b['status']}] {b['loaded_at']} — "
                f"저장 {b['total_rows_loaded']:,} / 격리 {b['total_rows_quarantined']:,}"
            )
        print(f"- 등록 데이터셋: {len(catalog['datasets'])}건")
        for ds in catalog["datasets"][:10]:
            print(
                f"  * {ds['dataset_id']} | {ds['target_table']} ({ds['dataset_kind_label']}) "
                f"| 원본={ds['source_name']} | 저장 {ds['rows_loaded']:,}건"
            )
        if catalog["quarantine"]:
            print(f"- 격리함 오류 행: {len(catalog['quarantine'])}건")
    return 0


def _run_parse(args: argparse.Namespace) -> int:
    # 무거운 파서 의존성은 parse 명령이 실행될 때만 지연 import한다.
    from autoetl.parser import (
        ChunkOptions,
        LibreOfficePptxRenderer,
        ParseOptions,
        ParserInputError,
        VisionStatus,
        chunk_document,
        create_layout_detector,
        create_vision_adapter,
        inspect_document,
        parse_and_export,
        parse_batch,
        parse_document,
        parse_page_selection,
        render_chunks_jsonl,
        render_document_markdown,
        render_routing_report_markdown,
    )

    if not args.file.exists():
        print(f"입력 파일 또는 폴더가 없다: {args.file}", file=sys.stderr)
        return 2
    if args.file.is_dir() and args.out is None:
        print("폴더 일괄 처리에는 --out <출력폴더> 지정이 필요하다.", file=sys.stderr)
        return 2
    if args.out is not None and _targets_same_file(args.out, args.file):
        print("--out이 입력 경로와 같다. 원본을 덮어쓰지 않는다.", file=sys.stderr)
        return 2
    if args.dpi <= 0:
        print("--dpi는 양수여야 한다.", file=sys.stderr)
        return 2
    if args.workers <= 0:
        print("--workers는 1 이상이어야 한다.", file=sys.stderr)
        return 2
    if not (0.0 < args.layout_score_threshold <= 1.0):
        print("--layout-score-threshold는 0보다 크고 1 이하여야 한다.", file=sys.stderr)
        return 2
    if args.chunk_max_chars < 200:
        print("--chunk-max-chars는 200 이상이어야 한다.", file=sys.stderr)
        return 2
    if args.chunk_overlap < 0:
        print("--chunk-overlap은 0 이상이어야 한다.", file=sys.stderr)
        return 2
    if args.vision != "none" and args.no_crops and not args.plan_only:
        print(
            "--vision을 사용할 때는 이미지 크롭이 필요하므로 --no-crops와 함께 쓸 수 없다.",
            file=sys.stderr,
        )
        return 2

    pptx_renderer: LibreOfficePptxRenderer | None = None
    try:
        pages = parse_page_selection(args.pages) if args.pages else None
        options = ParseOptions(
            pages=pages,
            extract_crops=not args.no_crops,
            render_dpi=args.dpi,
            detect_borderless_tables=not args.no_borderless_tables,
            layout_detect_all_pages=args.layout_all_pages,
            include_speaker_notes=not args.no_speaker_notes,
            include_comments=args.include_comments,
        )
        chunk_opts = ChunkOptions(
            max_chars=args.chunk_max_chars,
            overlap_chars=args.chunk_overlap,
        )
        if args.pptx_renderer == "libreoffice":
            pptx_renderer = LibreOfficePptxRenderer()
            if not pptx_renderer.available:
                raise ParserInputError(
                    "LibreOffice PPTX 렌더러를 쓸 수 없다 — "
                    "`soffice`/`libreoffice`와 PDF 렌더러(`pypdfium2` 또는 `pdftoppm`)가 필요하다."
                )

        layout_detector = (
            create_layout_detector(
                args.layout_detector,
                model_path=args.layout_model_path,
                label_preset=args.layout_label_preset,
                base_url=args.layout_base_url,
                api_key_env=args.layout_api_key_env,
                score_threshold=args.layout_score_threshold,
                allow_external=args.allow_external_layout,
                registered_internal=tuple(args.vision_internal_host or ()),
            )
            if args.layout_detector != "none"
            else None
        )

        vision_adapter = (
            create_vision_adapter(
                args.vision,
                model=args.vision_model,
                base_url=args.vision_base_url,
                api_key_env=args.vision_api_key_env,
                ocr_lang=args.ocr_lang,
                prompt_preset=args.vision_prompt_preset,
                allow_external=args.allow_external_vision,
                registered_internal=tuple(args.vision_internal_host or ()),
            )
            if (args.vision != "none" and not args.plan_only)
            else None
        )

        # 폴더 입력: 일괄 배치 처리 경로
        if args.file.is_dir():
            include_exts = (
                tuple(tok.strip() for tok in args.include_ext.split(",") if tok.strip())
                if args.include_ext
                else None
            )
            batch_manifest = parse_batch(
                args.file,
                args.out,
                recursive=not args.no_recursive,
                include_extensions=include_exts,
                workers=args.workers,
                resume=args.resume,
                fail_fast=args.fail_fast,
                plan_only=args.plan_only,
                options=options,
                include_chunks=not args.no_chunks,
                chunk_options=chunk_opts,
                vision_adapter=vision_adapter,
                page_renderer=pptx_renderer,
                layout_detector=layout_detector,
            )
            mode_lbl = "일괄 진단" if args.plan_only else "일괄 파싱"
            print(
                f"{mode_lbl} 완료: {args.file} -> {args.out} "
                f"(총 {batch_manifest.total_files}건: 성공 {batch_manifest.succeeded}, "
                f"재개 스킵 {batch_manifest.skipped_resume}, 실패 {batch_manifest.failed}, "
                f"{batch_manifest.elapsed_seconds:.2f}s)"
            )
            print(f"- 요약 리포트(MD): {args.out / 'batch_summary.md'}")
            print(f"- 매니페스트(JSON): {args.out / 'batch_manifest.json'}")
            if batch_manifest.all_chunks_jsonl:
                print(
                    f"- 통합 RAG 청크: {args.out / batch_manifest.all_chunks_jsonl} "
                    f"({batch_manifest.total_chunks}개)"
                )
            return 1 if (args.fail_fast and batch_manifest.failed > 0) else 0

        if args.plan_only:
            plan = inspect_document(
                args.file,
                options=options,
                layout_detector=layout_detector,
                page_renderer=pptx_renderer,
            )
            if args.out is not None:
                out_dir: Path = args.out
                out_dir.mkdir(parents=True, exist_ok=True)
                md_path = out_dir / "routing_report.md"
                json_path = out_dir / "routing_report.json"
                md_path.write_text(render_routing_report_markdown(plan), encoding="utf-8")
                json_path.write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
                print(
                    f"구조 진단 완료: {plan.document.source_path} "
                    f"({plan.summary.analyzed_pages}/{plan.summary.total_pages}p, "
                    f"표 {plan.summary.table_count}개, 시각 {plan.summary.visual_region_count}개)"
                )
                print(f"- 전략 리포트(MD): {md_path}")
                print(f"- 전략 리포트(JSON): {json_path}")
            elif args.format == "json":
                print(plan.model_dump_json(indent=2))
            else:
                print(render_routing_report_markdown(plan), end="")
            return 0

        if args.out is not None:
            doc, manifest = parse_and_export(
                args.file,
                args.out,
                options=options,
                vision_adapter=vision_adapter,
                page_renderer=pptx_renderer,
                layout_detector=layout_detector,
                include_chunks=not args.no_chunks,
                chunk_options=chunk_opts,
            )
            s = doc.routing_plan.summary
            print(
                f"파싱 완료: {doc.document.source_path} "
                f"({s.analyzed_pages}/{s.total_pages}p, "
                f"표 {len(doc.tables)}개, 시각 {len(doc.visual_regions)}개)"
            )
            print(f"- 통합 IR JSON: {manifest.parsed_json}")
            print(f"- 통합 Markdown: {manifest.document_markdown}")
            if manifest.chunks_jsonl is not None:
                print(f"- RAG 청크 JSONL: {manifest.chunks_jsonl} ({manifest.chunk_count}개)")
            print(f"- 구조·전략 리포트: {manifest.routing_report_markdown}")
            print(f"- 전략 JSON: {manifest.routing_report_json}")
            if layout_detector is not None:
                print(f"- 레이아웃 탐지: {layout_detector.name}")
            if manifest.table_csv_paths:
                tables_dir = manifest.out_dir / "tables"
                print(f"- 추출 표 CSV/JSON: {len(manifest.table_csv_paths)}개 ({tables_dir})")
            if manifest.crop_paths:
                crops_dir = manifest.out_dir / "crops"
                print(f"- 시각 영역 크롭: {len(manifest.crop_paths)}개 ({crops_dir})")
            if vision_adapter is not None and doc.visual_regions:
                done_cnt = sum(
                    1 for v in doc.visual_regions if v.vision_status == VisionStatus.COMPLETED
                )
                fail_cnt = sum(
                    1 for v in doc.visual_regions if v.vision_status == VisionStatus.FAILED
                )
                print(
                    f"- 시각 해석({vision_adapter.name}): "
                    f"완료 {done_cnt}/{len(doc.visual_regions)}개 (실패 {fail_cnt}개)"
                )
            return 0

        doc = parse_document(
            args.file,
            options=options,
            vision_adapter=vision_adapter,
            page_renderer=pptx_renderer,
            layout_detector=layout_detector,
        )
        if args.format == "json":
            print(doc.model_dump_json(indent=2))
        elif args.format == "markdown":
            print(render_document_markdown(doc), end="")
        elif args.format == "chunks":
            print(render_chunks_jsonl(chunk_document(doc, options=chunk_opts)), end="")
        else:
            print(render_routing_report_markdown(doc.routing_plan), end="")
        return 0
    except ParserInputError as exc:
        print(f"문서를 처리할 수 없다: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "문서 파싱이 내부 오류로 끝났다(문서 본문 보호를 위해 상세 원문은 표시하지 않는다).",
            file=sys.stderr,
        )
        return 3
    finally:
        if pptx_renderer is not None:
            pptx_renderer.close()


def _run_doctor(args: argparse.Namespace) -> int:
    from autoetl.doctor import collect_doctor_report, render_doctor_markdown

    report = collect_doctor_report()
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(render_doctor_markdown(report), end="")
    return 0


def _run_ui(args: argparse.Namespace) -> int:
    from autoetl.parser.adapters import ParserInputError
    from autoetl.ui import serve_ui

    if not (0 <= args.port <= 65535):
        print("--port는 0~65535 범위여야 한다.", file=sys.stderr)
        return 2
    try:
        serve_ui(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return 0
    except ParserInputError as exc:
        print(f"로컬 웹 UI를 시작할 수 없다: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"포트 바인딩에 실패했다({args.host}:{args.port}): {exc}", file=sys.stderr)
        return 1


def _run_index(args: argparse.Namespace) -> int:
    from autoetl.docindex import build_document_index
    from autoetl.parser.adapters import ParserInputError

    inputs: list[Path] = list(args.inputs)
    for src in inputs:
        if not src.exists():
            print(f"인덱싱 대상 파일 또는 폴더가 없다: {src}", file=sys.stderr)
            return 2

    try:
        summary = build_document_index(
            inputs,
            args.out,
            recursive=not args.no_recursive,
            replace=args.replace,
        )
    except ParserInputError as exc:
        print(f"문서 인덱싱에 실패했다: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "문서 인덱싱 중 내부 오류가 발생했다(문서 본문 보호를 위해 상세 원문 생략).",
            file=sys.stderr,
        )
        return 3

    if args.json:
        print(summary.model_dump_json(indent=2))
    else:
        print(
            f"문서 원문 인덱스 구축 완료: {summary.db_path} "
            f"(신규 {summary.documents_indexed}건, 건너뜀 {summary.documents_skipped}건, "
            f"섹션 {summary.sections_indexed}개, 표 {summary.tables_indexed}개, "
            f"{summary.elapsed_seconds:.2f}s)"
        )
        for warn in summary.warnings:
            print(f"- 경고: {warn}")
    return 0


def _parse_synonyms_arg(raw: str | None) -> dict[str, list[str]] | None:
    """`--synonyms` 인자('휴가=연차|유급휴가,등록=신청' 또는 JSON)를 동의어 사전으로 파싱한다."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("{"):
        import json

        data = json.loads(text)
        if isinstance(data, dict):
            return {
                str(k): ([str(v)] if isinstance(v, str) else [str(x) for x in v])
                for k, v in data.items()
            }
    out: dict[str, list[str]] = {}
    for entry in text.split(","):
        if "=" not in entry:
            continue
        left, right = entry.split("=", 1)
        key = left.strip()
        vals = [v.strip() for v in right.split("|") if v.strip()]
        if key and vals:
            out.setdefault(key, []).extend(vals)
    return out or None


def _run_query(args: argparse.Namespace) -> int:
    from autoetl.docindex import (
        DocumentIndex,
        create_reranker,
        render_evidence_pack_markdown,
    )
    from autoetl.parser.adapters import ParserInputError

    db_path: Path = args.index_db
    if not db_path.is_file():
        print(f"문서 인덱스 DB 파일이 없다: {db_path}", file=sys.stderr)
        return 2
    if args.top_k <= 0:
        print("--top-k는 1 이상이어야 한다.", file=sys.stderr)
        return 2

    try:
        synonyms = _parse_synonyms_arg(getattr(args, "synonyms", None))
        reranker = create_reranker(
            args.reranker,
            model_path=args.reranker_model,
            tokenizer_path=args.reranker_tokenizer,
        )
        with DocumentIndex(db_path) as idx:
            pack = idx.search(
                args.question,
                top_k=args.top_k,
                candidate_k=max(args.top_k, args.candidate_k),
                doc_id=args.doc_id,
                reranker=reranker,
                synonyms=synonyms,
            )
    except ParserInputError as exc:
        print(f"문서 검색에 실패했다: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "문서 검색 중 내부 오류가 발생했다(문서 본문 보호를 위해 상세 원문 생략).",
            file=sys.stderr,
        )
        return 3

    if args.format == "json":
        print(pack.model_dump_json(indent=2))
    else:
        print(render_evidence_pack_markdown(pack), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autoetl", description="결정론 엔진과 LLM 하네스 기반 로컬 ETL·문서 파서"
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"autoetl {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser(
        "analyze",
        help=(
            "정형 파일·폴더(CSV·TSV·XLSX)의 칼럼 분류·문자열 분해·"
            "PK/FK 관계·최적 DB 적재 계획을 생성한다"
        ),
    )
    analyze.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="입력 파일(CSV·TSV·XLSX) 또는 폴더(복수 지정 가능)",
    )
    analyze.add_argument(
        "--purpose",
        choices=[purpose.value for purpose in Purpose],
        default=Purpose.OLTP.value,
        help="DB 적재 용도(기본: oltp — 채점 가중치·인덱스·파티셔닝 기준)",
    )
    analyze.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "번들 저장 폴더(ddl.sql, load_plan.json, relations.json, "
            "erd.mmd, analysis_report.md) 또는 .json 파일"
        ),
    )
    analyze.add_argument(
        "--format",
        choices=["report", "json", "ddl", "erd"],
        default="report",
        help="--out 미지정 시 stdout 출력 형식(기본: report)",
    )
    analyze.add_argument(
        "--no-decompose-strings",
        action="store_true",
        help="규칙성 문자열의 가변 슬롯 컬럼 분리·숨은 FK 구체화 비활성화",
    )
    analyze.add_argument(
        "--no-recursive",
        action="store_true",
        help="폴더 입력 시 하위 디렉터리 재귀 탐색 비활성화",
    )
    analyze.add_argument(
        "--no-llm",
        action="store_true",
        help="호환용 플래그(결정론 분석 엔진은 기본적으로 0MB AI/LLM 없이 동작)",
    )

    parse = subparsers.add_parser(
        "parse",
        help="문서 파일 또는 폴더(PDF·PPTX·DOCX·HWPX·XLSX·HWP·DOC·PPT)를 구조 분류·추출한다",
    )
    parse.add_argument(
        "file",
        type=Path,
        help="입력 문서 파일 또는 폴더(PDF·PPTX·DOCX·HWPX·XLSX·HWP·DOC·PPT 지원)",
    )
    parse.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "결과물 번들(parsed.json, document.md, chunks.jsonl, routing_report.*, "
            "tables/, crops/) 또는 배치 결과 저장 폴더"
        ),
    )
    parse.add_argument(
        "--plan-only",
        action="store_true",
        help="본문·크롭 추출 없이 구조 분류와 파싱 전략 리포트만 생성",
    )
    parse.add_argument(
        "--no-recursive",
        action="store_true",
        help="폴더 일괄 처리 시 하위 디렉터리 재귀 탐색 비활성화",
    )
    parse.add_argument(
        "--workers",
        type=int,
        default=1,
        help="폴더 일괄 처리 병렬 워커 수(기본: 1)",
    )
    parse.add_argument(
        "--resume",
        action="store_true",
        help="폴더 일괄 처리 시 동일 SHA-256으로 이미 완료된 문서 재파싱 건너뛰기",
    )
    parse.add_argument(
        "--fail-fast",
        action="store_true",
        help="폴더 일괄 처리 시 첫 실패 문서에서 즉시 중단(기본: 오류 격리 후 계속)",
    )
    parse.add_argument(
        "--include-ext",
        type=str,
        default=None,
        help="폴더 일괄 처리 시 특정 확장자만 필터링(쉼표 구분, 예: '.pdf,.hwp,.xlsx')",
    )
    parse.add_argument(
        "--pages",
        type=str,
        default=None,
        help="분석할 페이지/슬라이드 범위(예: '1-3,5'). 생략하면 전체",
    )
    parse.add_argument(
        "--format",
        choices=["report", "markdown", "json", "chunks"],
        default="report",
        help="--out 미지정 시 stdout 출력 형식(기본: report)",
    )
    parse.add_argument(
        "--no-chunks",
        action="store_true",
        help="번들 내보내기 시 RAG 구조화 청크(`chunks.jsonl`) 생성 생략",
    )
    parse.add_argument(
        "--chunk-max-chars",
        type=int,
        default=1500,
        help="RAG 청크 1개당 최대 문자 수(기본: 1500, 최소 200)",
    )
    parse.add_argument(
        "--chunk-overlap",
        type=int,
        default=150,
        help="동일 섹션 연속 산문 청크 간 오버랩 문자 수(기본: 150)",
    )
    parse.add_argument(
        "--no-crops",
        action="store_true",
        help="스캔·그림·도식 영역의 이미지 크롭 추출 생략",
    )
    parse.add_argument(
        "--no-borderless-tables",
        action="store_true",
        help="테두리선 없는 무선 표 휴리스틱 탐지 비활성화",
    )
    parse.add_argument(
        "--no-speaker-notes",
        action="store_true",
        help="PPTX 슬라이드 발표자 노트(Speaker Notes) 추출 생략",
    )
    parse.add_argument(
        "--include-comments",
        action="store_true",
        help="PPTX·DOCX·HWPX·HWP·DOC·XLSX 검토 주석(Comments·숨은설명) 추출 포함",
    )
    parse.add_argument(
        "--pptx-renderer",
        choices=["none", "libreoffice"],
        default="none",
        help="PPTX 벡터 도식·OLE·EMF 크롭 렌더링 백엔드(기본: none — 결정론 유지)",
    )
    parse.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="페이지 렌더러로 크롭할 때의 해상도 DPI(기본: 150)",
    )
    parse.add_argument(
        "--vision",
        choices=["none", "tesseract", "openai-compat", "ollama", "hybrid"],
        default="none",
        help="시각 영역(스캔·그림·차트·도식) OCR/VLM 어댑터(기본: none)",
    )
    parse.add_argument(
        "--vision-model",
        type=str,
        default=None,
        help=(
            "VLM 모델명(openai-compat·ollama·hybrid용, 예: glm-ocr, PaddleOCR-VL-1.6, qwen2.5vl:7b)"
        ),
    )
    parse.add_argument(
        "--vision-prompt-preset",
        choices=["auto", "generic", "glm-ocr", "paddleocr-vl", "deepseek-ocr"],
        default="auto",
        help=(
            "OCR·VLM 프롬프트 프리셋"
            "(기본: auto — 모델명에서 glm-ocr/paddleocr-vl/deepseek-ocr 자동 감지)"
        ),
    )
    parse.add_argument(
        "--vision-base-url",
        type=str,
        default=None,
        help="VLM 엔드포인트 URL(기본: openai-compat=http://127.0.0.1:11434/v1, ollama=http://127.0.0.1:11434)",
    )
    parse.add_argument(
        "--vision-api-key-env",
        type=str,
        default=None,
        help="API 키를 읽을 환경변수 이름(키 원문은 CLI 인자로 받지 않음)",
    )
    parse.add_argument(
        "--ocr-lang",
        type=str,
        default="kor+eng",
        help="Tesseract OCR 언어 설정(기본: kor+eng, 미설치 언어는 자동 폴백)",
    )
    parse.add_argument(
        "--allow-external-vision",
        action="store_true",
        help="루프백·등록 사내 서버 밖의 외부 VLM 엔드포인트로 이미지 전송을 명시적으로 허용",
    )
    parse.add_argument(
        "--vision-internal-host",
        action="append",
        default=None,
        help="신뢰 구역으로 등록할 사내 사설 IP 주소(반복 지정 가능)",
    )
    parse.add_argument(
        "--layout-detector",
        choices=["none", "heuristic", "onnx", "http"],
        default="none",
        help="스캔 페이지 레이아웃 객체 탐지기(기본: none — 결정론 유지)",
    )
    parse.add_argument(
        "--layout-model-path",
        type=Path,
        default=None,
        help="ONNX 레이아웃 모델 경로(--layout-detector onnx용, 예: PP-DocLayoutV2.onnx)",
    )
    parse.add_argument(
        "--layout-label-preset",
        choices=[
            "pp-doclayout-v2",
            "pp-doclayout-v3",
            "pp-doclayout-plus-l",
            "doclaynet",
            "publaynet",
        ],
        default="pp-doclayout-v2",
        help="ONNX 레이아웃 모델 라벨 프리셋(기본: pp-doclayout-v2)",
    )
    parse.add_argument(
        "--layout-base-url",
        type=str,
        default=None,
        help="HTTP 레이아웃 탐지 서비스 URL(기본: http://127.0.0.1:8001)",
    )
    parse.add_argument(
        "--layout-api-key-env",
        type=str,
        default=None,
        help="HTTP 레이아웃 서비스 API 키 환경변수 이름(키 원문은 CLI 인자로 받지 않음)",
    )
    parse.add_argument(
        "--layout-score-threshold",
        type=float,
        default=0.4,
        help="레이아웃 탐지 최소 신뢰도 임계값(0<t<=1, 기본: 0.4)",
    )
    parse.add_argument(
        "--layout-all-pages",
        action="store_true",
        help="스캔 페이지 외 디지털 페이지에도 레이아웃 탐지를 보조 적용",
    )
    parse.add_argument(
        "--allow-external-layout",
        action="store_true",
        help="루프백·등록 사내 서버 밖의 외부 HTTP 레이아웃 탐지 엔드포인트 전송을 명시적으로 허용",
    )

    doctor = subparsers.add_parser(
        "doctor",
        help=(
            "실행 환경과 지원 포맷·선택 백엔드(LibreOffice·렌더러·ONNX·Tesseract) 가용성을 진단한다"
        ),
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="진단 결과를 JSON 형식으로 출력",
    )

    ui = subparsers.add_parser(
        "ui",
        help="무의존성 로컬 전용(127.0.0.1) Auto Parser 웹 UI 서버를 기동한다",
    )
    ui.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="바인딩 호스트(보안상 기본 루프백 127.0.0.1만 허용)",
    )
    ui.add_argument(
        "--port",
        type=int,
        default=8765,
        help="로컬 HTTP 포트(기본: 8765, 0 지정 시 가용 포트 자동 할당)",
    )
    ui.add_argument(
        "--no-browser",
        action="store_true",
        help="서버 시작 시 기본 브라우저 자동 열기 비활성화",
    )

    index_cmd = subparsers.add_parser(
        "index",
        help=(
            "파싱된 번들(parsed.json) 또는 원본 문서를 VectorDB 없는 "
            "SQLite FTS5 원문 인덱스로 구축한다"
        ),
    )
    index_cmd.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="인덱싱할 `parsed.json`, 원본 문서 파일, 또는 폴더(복수 지정 가능)",
    )
    index_cmd.add_argument(
        "--out",
        type=Path,
        required=True,
        help="생성/갱신할 SQLite 원문 인덱스 파일 경로(예: ./doc_index.sqlite)",
    )
    index_cmd.add_argument(
        "--no-recursive",
        action="store_true",
        help="폴더 입력 시 하위 디렉터리 재귀 탐색 비활성화",
    )
    index_cmd.add_argument(
        "--replace",
        action="store_true",
        help="동일 경로(source_path) 또는 동일 SHA-256 문서가 이미 있으면 구버전을 원자적으로 교체",
    )
    index_cmd.add_argument(
        "--json",
        action="store_true",
        help="인덱싱 요약을 JSON 형식으로 출력",
    )

    query_cmd = subparsers.add_parser(
        "query",
        help=(
            "SQLite FTS5 원문 인덱스(+선택 초경량 CPU ONNX 리랭커)에서 "
            "원문 근거(EvidencePack)를 검색한다"
        ),
    )
    query_cmd.add_argument("index_db", type=Path, help="문서 인덱스 SQLite 파일 경로")
    query_cmd.add_argument("question", type=str, help="검색할 질문 또는 키워드")
    query_cmd.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="반환할 상위 원문 섹션 수(기본: 5)",
    )
    query_cmd.add_argument(
        "--candidate-k",
        type=int,
        default=20,
        help="1단계 FTS5 후보 추출 수(기본: 20)",
    )
    query_cmd.add_argument(
        "--doc-id",
        type=str,
        default=None,
        help="특정 문서로 검색 범위 제한",
    )
    query_cmd.add_argument(
        "--synonyms",
        type=str,
        default=None,
        help="도메인 동의어 확장(예: '휴가=연차|유급휴가,등록=신청' 또는 JSON 객체)",
    )
    query_cmd.add_argument(
        "--reranker",
        choices=["none", "onnx"],
        default="none",
        help="2단계 Cross-Encoder 리랭커(기본: none — 0MB FTS5 BM25+목차)",
    )
    query_cmd.add_argument(
        "--reranker-model",
        type=Path,
        default=None,
        help="CPU ONNX 리랭커 모델 경로(예: bge-reranker-v2-m3-int8.onnx)",
    )
    query_cmd.add_argument(
        "--reranker-tokenizer",
        type=Path,
        default=None,
        help="리랭커 tokenizer.json 경로(생략 시 모델과 같은 폴더의 tokenizer.json 사용)",
    )
    query_cmd.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="출력 형식(기본: markdown)",
    )

    load_cmd = subparsers.add_parser(
        "load",
        help=(
            "정형 파일·폴더(CSV·TSV·XLSX)를 결정론 분석 후 SQLite/DuckDB/PostgreSQL DB에 "
            "직접 적재하고 기본 데이터 관리 테이블(_etl_*)을 기록한다"
        ),
    )
    load_cmd.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="적재할 파일(CSV·TSV·XLSX) 또는 폴더(복수 지정 가능)",
    )
    load_cmd.add_argument(
        "--db",
        type=str,
        required=True,
        help="대상 DB 파일 경로(예: ./warehouse.sqlite, ./analytics.duckdb) 또는 PostgreSQL DSN",
    )
    load_cmd.add_argument(
        "--engine",
        choices=["sqlite", "duckdb", "postgres"],
        default="sqlite",
        help="DB 엔진(기본: sqlite — 무의존성 로컬 파일 DB)",
    )
    load_cmd.add_argument(
        "--purpose",
        choices=[purpose.value for purpose in Purpose],
        default=Purpose.OLTP.value,
        help="DB 적재 용도(기본: oltp)",
    )
    load_cmd.add_argument(
        "--mask-pii",
        action="store_true",
        help="탐지된 주요 개인정보(L2/L3 PII) 컬럼을 적재 시 자동 마스킹",
    )
    load_cmd.add_argument(
        "--on-error",
        choices=["quarantine", "reject_all"],
        default="quarantine",
        help="변환 오류·고아 FK 행 처리 정책(기본: quarantine=격리함 분리, reject_all=전체 중단)",
    )
    load_cmd.add_argument(
        "--overrides",
        type=Path,
        default=None,
        help="컬럼명·UNIQUE·마스킹 확정 JSON 파일 경로(SchemaOverrideSpec 형식)",
    )
    load_cmd.add_argument(
        "--no-audit-columns",
        action="store_true",
        help="업무 테이블의 행 단위 이력 컬럼(_etl_batch_id/_etl_dataset_id/_etl_loaded_at) 생략",
    )
    load_cmd.add_argument(
        "--no-management-tables",
        action="store_true",
        help="디폴트 데이터 관리 테이블(_etl_load_batches/_etl_datasets 등) 생성 생략",
    )
    load_cmd.add_argument(
        "--no-decompose-strings",
        action="store_true",
        help="규칙성 문자열의 가변 슬롯 컬럼 분리 비활성화",
    )
    load_cmd.add_argument(
        "--no-recursive",
        action="store_true",
        help="폴더 입력 시 하위 디렉터리 재귀 탐색 비활성화",
    )
    load_cmd.add_argument(
        "--force",
        action="store_true",
        help="동일 파일(SHA-256 일치)이 이미 적재되어 있어도 강제로 다시 적재",
    )
    load_cmd.add_argument(
        "--notes",
        type=str,
        default="",
        help="배치 적재 이력(_etl_load_batches.notes)에 남길 메모",
    )
    load_cmd.add_argument(
        "--json",
        action="store_true",
        help="적재 결과 요약을 JSON 형식으로 출력",
    )

    catalog_cmd = subparsers.add_parser(
        "catalog",
        help=(
            "적재된 DB의 데이터 관리 대장(_etl_load_batches/_etl_datasets/격리함) 조회 및 배치 롤백"
        ),
    )
    catalog_cmd.add_argument(
        "db",
        type=str,
        help="조회할 DB 파일 경로(.sqlite/.duckdb) 또는 PostgreSQL DSN",
    )
    catalog_cmd.add_argument(
        "--engine",
        choices=["sqlite", "duckdb", "postgres"],
        default="sqlite",
        help="DB 엔진(기본: sqlite)",
    )
    catalog_cmd.add_argument(
        "--rollback",
        type=str,
        default=None,
        help="지정한 batch_id로 적재된 모든 행을 자식→부모 역순으로 되돌리기",
    )
    catalog_cmd.add_argument(
        "--limit",
        type=int,
        default=30,
        help="최근 조회할 배치·데이터셋·격리 행 최대 건수(기본: 30)",
    )
    catalog_cmd.add_argument(
        "--json",
        action="store_true",
        help="카탈로그/롤백 결과를 JSON 형식으로 출력",
    )

    key_cmd = subparsers.add_parser("key", help="로컬 ETL 마스킹 키 상태 조회 및 백업·이전")
    key_subparsers = key_cmd.add_subparsers(dest="key_command", required=True)
    key_status = key_subparsers.add_parser("status", help="로컬 키 파일과 키 ID 상태 조회")
    key_status.add_argument("--db", type=Path, help="로컬 키 ID와 비교할 SQLite 또는 DuckDB 파일")
    key_export = key_subparsers.add_parser("export", help="마스킹 키를 새 백업 파일로 복사")
    key_export.add_argument("file", type=Path, help="백업 파일 경로")
    key_import = key_subparsers.add_parser("import", help="백업 파일에서 마스킹 키 가져오기")
    key_import.add_argument("file", type=Path, help="가져올 키 파일 경로")
    key_import.add_argument(
        "--replace", action="store_true", help="이전 키의 백업을 만든 뒤 다른 키로 교체"
    )

    args = parser.parse_args(argv)
    if args.command == "analyze":
        return _run_analyze(args)
    if args.command == "load":
        return _run_load(args)
    if args.command == "catalog":
        return _run_catalog(args)
    if args.command == "key":
        return _run_key(args)
    if args.command == "parse":
        return _run_parse(args)
    if args.command == "index":
        return _run_index(args)
    if args.command == "query":
        return _run_query(args)
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "ui":
        return _run_ui(args)
    parser.error("알 수 없는 명령")


if __name__ == "__main__":
    sys.exit(main())
