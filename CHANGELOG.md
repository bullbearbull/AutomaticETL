# Changelog

이 문서는 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 따릅니다.

## [Unreleased]

### Added

- **결정론 다중 테이블 ETL 분석·관계도·최적 DB 적재 계획 엔진 (0MB AI, Milestones E1–E5)**:
  - 다중 파일·다중 시트 격리 DuckDB 컨텍스트(`MultiTableContext`): CSV·TSV·다중 시트 XLSX·메모리 표를 단일 잠금 DuckDB 세션의 스키마별(`t0..tN`) 테이블로 일괄 적재
  - 10종 정밀 칼럼 유형 분류 및 S4 심화 통계(`s04_stats`, `column_classifier`): 인덱스/순번, 이산·연속 수치형, 명목·서열 범주형, 날짜(한국어 날짜 포함), 규칙 문자열, 코드, 자유 텍스트, 불리언 및 결측치 프로파일링
  - 결정론 규칙 문자열 내부 분해기(`analyze_string_pattern`): 공통 고정부와 가변 슬롯 분리, 슬롯별 세부 유형·정규식·DuckDB/PostgreSQL `regexp_extract` 추출식 생성
  - 내부·교차 테이블 관계 분석 및 최적 PK 선정(`relations_multi`, `pk_selector`): FD·상관·산술 파생·범주 계층, 테이블 간 FK(규칙 문자열 가변 슬롯 속 숨은 FK 포함), 피참조 횟수 기반 단일/복합 PK 및 서로게이트 키 권고
  - 최적 DB 스키마 채점·위상 적재 플랜·번들 내보내기(`load_planner`, `analyze_tables`): 목적(`oltp`/`analytics`/`archive`)별 후보 채점, `sqlglot` 재파싱 검증 PostgreSQL DDL + 인덱스 DDL, Kahn 위상 적재 순서, `UPSERT`/`WATERMARK`/`APPEND` 증분 전략, Mermaid ERD(`erd.mmd`) 및 한국어 분석 리포트(`analysis_report.md`)
  - CLI(`autoetl analyze <dir|files...> --out ./etl_plan`), 최상위 Python SDK(`autoetl.analyze_tables`), 로컬 웹 UI(`autoetl ui` 다중 테이블 ETL 분석 모드 + `/api/analyze-tables` + ZIP 다운로드)
- **VectorDB 없는 원문 문서 인덱스·리트리벌 및 선택 초경량 CPU ONNX 리랭커 (Milestones D1–D2)**:
  - `src/autoetl/docindex/`: 0MB 기본 **SQLite FTS5(한글 2-gram + 공백 혼합 토크나이저) + 제목 계층 목차 트리 + 원문 섹션 전체 조회 + 격리 DuckDB 표 SQL(`SELECT/WITH` 읽기 전용 검증)** 엔진(`DocumentIndex`, `build_document_index`, `EvidencePack`)
  - 선택 초경량 CPU ONNX Cross-Encoder 리랭커(`OnnxCrossEncoderReranker`): `bge-reranker-v2-m3(-ko)` / `bge-reranker-base` 등 100M–280M INT8 모델 지연 로딩, 장문 슬라이딩 창 최대 점수 집계, 원문 전체 무손실 반환
  - 프레임워크 비종속 에이전트/MCP 표준 도구 인터페이스(`DocumentToolkit` — `search_documents`, `get_outline`, `read_section`, `list_tables`, `query_table` 5종 + JSON Schema 스펙 + 단일 디스패처) 및 CLI(`autoetl index`, `autoetl query`)

## [0.1.0-beta.1] — 2026-11-23 (`0.1.0b1`)

Auto Parser 중심의 첫 번째 베타 릴리스.

### Added

- **8개 문서 포맷 파서 (`src/autoetl/parser/`)**:
  - PDF (`.pdf`): `pypdf` + 자체 기하·표·이미지 추출기, 다단 읽기 순서, 격자/가로선/무선 표, 스캔 페이지 크롭, 선택 레이아웃 탐지(`Heuristic`/`ONNX PP-DocLayout`/`HTTP`) 및 OCR/VLM(`Tesseract`/`Ollama`/`OpenAI-compat`/`Hybrid`) 어댑터
  - PPTX (`.pptx`): 내장 순수 Python OOXML 파서 — 레이아웃 상속, 그룹 도형, 병합 표, 차트(`chart`/`chartEx`), 내장 `.xlsx`, SmartArt, 발표자 노트/주석, 선택 LibreOffice 렌더러
  - DOCX (`.docx`): 내장 순수 Python OOXML 파서 — 스타일 상속, 다단계 번호, 변경추적(`w:ins`/`w:del`), 병합/중첩 표, OMML 수식, 각주·미주·주석·머리글/바닥글
  - HWPX (`.hwpx`): 내장 순수 Python OWPML 파서 — 개요/번호, 격자 병합 표, 한컴 수식, 머리글/바닥글/각주/미주/숨은주석 분리, 차트·이미지
  - XLSX (`.xlsx`): 내장 순수 Python 스트리밍 파서 — `sharedStrings` 루비 제거, 날짜(1900/1904)·백분율·천단위·고정밀 소수 보존, 병합 셀·다단 헤더, 주석·차트·이미지, 암호화 OLE2 차단
  - 구형 바이너리 OLE2 (`.hwp`, `.doc`, `.ppt`): 내장 순수 Python MS-CFB 리더 + HWP 5.0 네이티브 파서 + Word/PowerPoint 97–2003 하이브리드 파서(LibreOffice 설치 시 자동 승격, 미설치 시 내장 폴백)
- **RAG 청킹 (`chunks.jsonl`)**: 제목 계층 경로(`section_path`), 페이지 범위, 표 행 무결성 분할(헤더 자동 반복), 제목 전용 섹션 보존, VLM 텍스트 통합
- **폴더 일괄 배치 (`parse_batch`)**: `autoetl parse <dir> --out <out>`, `--workers`, `--resume`(SHA-256), 오류 격리, 통합 `batch_summary.md`·`batch_manifest.json`·`all_chunks.jsonl`
- **로컬 웹 UI (`autoetl ui`)**: 무의존성 표준 라이브러리 `ThreadingHTTPServer`, `127.0.0.1` 루프백 전용 기본 강제, 오프라인 SPA, 5개 결과 탭 및 번들 ZIP 다운로드
- **환경 진단 및 Python SDK**: `autoetl doctor [--json]`, `autoetl --version`, 최상위 지연 로딩 SDK (`import autoetl`)
- **정형 CSV 분석 v0 (`autoetl analyze`)**: `--no-llm` 결정론 CSV 프로파일링 프로토타입
