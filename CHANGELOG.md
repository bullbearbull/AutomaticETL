# Changelog

이 문서는 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 따릅니다.

## [Unreleased]

### Fixed

- **정형 ETL 실제 시험 적재 검증 및 정직한 무결성 채점 (`load_planner.py`, `multi_analyze.py`, `contracts/schema.py`)**:
  - `ValidationSummary`가 더 이상 DDL 파싱 성공만으로 `trial_load_ok=true`/`constraint_violation_count=0`을 하드코딩하지 않고, 격리 DuckDB에서 실제 변환·캐스팅·`VARCHAR` 길이·`INT4` 범위·PK 유일성·물리 FK anti-join 시험 적재를 수행해 `trial_load_executed`, `cast_failure_count`, `pk_violation_count`, `fk_violation_count`, `data_loss_rate`를 정직하게 산출
  - 물리 FK 고아 행(`orphan_rows > 0`)이 존재하면 `trial_load_ok=false`, 무결성 점수 감점, 고아 행 정제·격리 경고 자동 출력
- **PostgreSQL DDL · 적재 플랜 · ERD 정합성 및 실행 가능성 보완 (`core/ddl/postgres.py`, `load_planner.py`)**:
  - `DECIMAL` 물리 타입(`balance` 등 천단위 쉼표 포함 숫자)이 의미 타입과 무관하게 `strip_currency_and_comma` 변환식을 생성하도록 수정(`$` 통화 기호 포함)
  - 서로게이트 PK에 `BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL`을 생성·검증해 실제 PostgreSQL `INSERT` 시 `23502` NOT NULL 오류 방지
  - 표본에서 우연히 유일한 컬럼(`postal_code` 등)을 `CREATE UNIQUE INDEX`로 자동 승격하지 않고 리포트·노트의 '표본 관측 유일 후보' 권고로 전환해 운영 적재 시 `23505` 충돌 방지
  - `resolve_postgres_column_types` 단일 진실 원천(SSOT)을 도입해 `ddl.sql`, `load_plan.json`의 `type_cast`, `erd.mmd`의 타입·정밀도·길이가 100% 일치하도록 통일
  - `C3_ANALYTICS` 선언적 파티셔닝을 실제 실행 가능한 `PARTITION BY RANGE (...)` DDL(파티션 키를 복합 PK에 포함, 하위 단일 FK 참조 부모 테이블은 보류)로 정합화
  - 해시 치환된 한글·비ASCII 컬럼에 `COMMENT ON COLUMN` DDL을 자동 생성하고 `TableLoadStrategy.source_encoding`에 감지 인코딩(CP949/UTF-8 등) 기록
- **엄격한 타입 판별·패턴 분해·예외 처리 개선 (`builtin.py`, `s03_types.py`, `string_pattern.py`, `column_classifier.py`, `multi_context.py`)**:
  - `BOOLEAN` 탐지를 정규 쌍(`{Y,N}`, `{Yes,No}`, `{예,아니오}`, `{O,X}`, `{True,False}`) 매칭으로 엄격화해 `{Y,N,x}`/`{x,y}` 오분류 차단
  - 단일 연도 표본(예: `2026`만 존재)에서도 연도·년월·년월일 토큰을 고정 리터럴로 묶지 않고 가변 슬롯 `{year}`(`([0-9]{4})`)로 유지하며, 추출식을 `NULLIF(regexp_extract(...), '')`로 감싸 미매칭 시 빈 문자열 대신 `NULL` 반환
  - 2자리 소수 고유값(`01`/`02`)의 `month` 오명명 방지, 전화번호 컬럼 과도 분해 차단, 1:1 단일 슬롯 중복 컬럼(`customer_id_code_0`) 및 OLTP 이행적 슬롯 FK 생략
  - 헤더만 존재하는 빈 CSV 입력 시 내부 오류 대신 명확한 한국어 안내 `InputError` 반환
- **PDF 배경색 헤더 표 첫 데이터 행 누락 수정 (`parser/pdf/geometry.py`, `extractor.py`, `tables.py`)**:
  - PDF 비선 색상 연산자(`g`/`rg`/`k`)를 추적해 흰색·연회색 바탕 및 표 전체 배경 사각형을 제외하고, 헤더 음영·색상·굵기·수치 신호를 종합 판정해 1행 헤더 표의 첫 데이터 행이 헤더로 흡수되는 현상 수정
- **원문 문서 인덱스 `--replace` 경로 교체 및 검색 투명성 보완 (`docindex/store.py`, `cli.py`)**:
  - `autoetl index --replace`가 `sha256`뿐 아니라 동일 `source_path` 구버전 문서도 단일 SQLite 트랜잭션 안에서 원자적으로 삭제·교체하도록 수정(파싱 실패 시 기존 버전 유지)
  - 소규모 코퍼스(문서 1~2건) 음수 IDF에 대한 어휘 커버리지 보정 및 BM25 소수점 4자리 출력, 도메인 동의어 확장(`synonyms`/`--synonyms`), 후보 0건 시 `fts5_no_candidates` 모드 및 안내 메시지 추가

### Added

- **S7 결정론적 PII·민감 컬럼 탐지 엔진 구현 (`core/stages/s07_sensitivity.py`)**:
  - 주민등록번호·외국인등록번호·사업자등록번호·카드번호(Luhn)·계좌번호·전화번호·이메일·성명·생년월일·주소·IP를 원본 값 노출 없이 결정론 탐지해 `analyze` 리포트·`TableLoadStrategy.sensitive_columns`·`security_isolation` 점수에 연동
- **결정론 스키마 마지막 10% 확정(`SchemaOverrideSpec`) + 실제 DB 적재 엔진 및 디폴트 데이터 관리 테이블 4종 (`core/db_loader.py`, `load_planner.py`, `cli.py`, `parser/ui/`)**:
  - 추가 코어 의존성 0개로 **SQLite(`.sqlite`) · DuckDB(`.duckdb`) · 선택 PostgreSQL(`psycopg`)**에 실제 변환·캐스팅·PII 마스킹/해시·위상 적재(`UPSERT`/`WATERMARK`/`APPEND`)를 수행하는 `load_tables` / `autoetl load` 구현
  - 사용자가 UI·CLI·SDK에서 DB 저장 영문 컬럼명, 물리 `UNIQUE` 제약 확정(시험 적재 중복 검증 연동), 개인정보 보호 정책(`none`/`mask`/`hash`), 오류·고아 행 정책(`quarantine`/`reject_all`)을 확정·재검증할 수 있도록 확장
  - 모든 DB 적재 시 기본 생성되는 **데이터 관리·리니지 테이블 4종**(`_etl_load_batches` 적재 배치 이력, `_etl_datasets` 데이터 고유 ID·데이터 종류 자동 분류·원본 SHA-256·스키마 지문, `_etl_column_registry` 원본 한글명↔DB 영문명·역할·PII 사전, `_etl_quarantine_rows` 오류·고아 행 PII 안전 격리함)과 행 단위 출처 컬럼(`_etl_batch_id`, `_etl_dataset_id`, `_etl_loaded_at`) 기본 적용
  - 동일 파일 중복 적재 방지(SHA-256), 스키마 변경(드리프트) 감지 및 신규 컬럼 자동 진화, 카탈로그 조회(`inspect_db_catalog` / `autoetl catalog`), FK 무결성 보호 역순 배치 롤백(`rollback_batch` / `autoetl catalog --rollback`) 구현

### Changed

- **로컬 웹 UI 전면 개편 — 직관적 흐름·세부 설정 접기·쉬운 한국어 네이밍·1클릭 DB 저장 (`parser/ui/app.html`, `parser/ui/server.py`)**:
  - 상단 3모드를 `문서 글·표 추출` · `엑셀·CSV 구조 분석` · `문서 내용 검색`으로 단순화하고, 파일 드래그앤드롭과 단일 실행 버튼만 전면에 배치(페이지 범위·청크 크기·코드 분리·AI 리랭커·SQL 등 고급 옵션은 `세부 설정` 접이식으로 정리)
  - `OLTP/3NF`, `기수성`, `서로게이트 키`, `워터마크`, `EvidencePack`, `BM25` 등 전문 용어를 일상 한국어(`일반 서비스용`, `표 연결 관계`, `자동 번호 키`, `날짜 기준 추가`, `관련도 %`)로 전면 개선하고, DuckDB 검증·개인정보 감지 배지·동의어 입력·문서 속 표 즉시 미리보기 추가
  - `엑셀·CSV 구조 분석` 내 인라인 항목 확정(DB 저장 이름·중복 금지·개인정보 보호 정책, 재업로드 없는 세션 재검증) 및 `DB로 저장하기` 탭(`POST /api/etl/load-db`, `.sqlite`/`.duckdb` 내려받기, 데이터 관리 대장·격리함 실시간 조회) 추가

## [0.2.0-beta.2] — 2026-11-24 (`0.2.0b2`)

로컬 웹 UI 3번째 탭(원문 문서 인덱스·검색·목차 뷰어·DuckDB 표 SQL)과 실측 기반 CPU ONNX 리랭커 최적화를 포함한 베타 릴리스.

### Added

- **로컬 웹 UI 3번째 모드 — 원문 문서 인덱스 · 검색 (`autoetl ui`)**:
  - 다중 문서(PDF·DOCX·PPTX·XLSX·HWPX·HWP·DOC·PPT) 업로드 및 SQLite FTS5 인덱스 구축(`POST /api/docindex/build`) + `.sqlite` 인덱스 다운로드(`/api/download/<token>`)
  - 0MB FTS5 및 선택 로컬 CPU ONNX 리랭커 전환 원문 검색(`POST /api/docindex/query`), 클릭형 목차 트리·원문 전체 뷰어(`POST /api/docindex/section`), 추출 표 격리 DuckDB 읽기 전용 SQL 플레이그라운드(`POST /api/docindex/table-sql`)
- **`autoetl doctor` 원문 문서 인덱스 및 선택 CPU ONNX 리랭커(`onnxruntime`·`tokenizers`) 준비 상태 점검 항목 추가**

### Changed

- **CPU ONNX Cross-Encoder 리랭커 실전 최적화 (`src/autoetl/docindex/reranker.py`)**:
  - HuggingFace 표준 `onnx/model_int8.onnx` 상위 폴더 `tokenizer.json` 자동 탐색 및 아키텍처별 패딩 토큰 ID(`XLM-RoBERTa <pad>=1`, `BERT [PAD]=0`) 동적 판별
  - 30KB 이상 장문 섹션 전체에서 질의 어휘 밀집 창을 우선 선별하는 질의 기반 슬라이딩 창(`max_windows=3`)과 길이 버킷 정렬 배치 + `ORT_ENABLE_ALL` 적용으로 2-코어 CPU 추론 속도 26~35% 단축 및 장문 후반부 재현율 개선
  - `jinaai/jina-reranker-v2-base-multilingual` (INT8 ~280MB, 한국어·다국어) 권장 모델 목록 추가

## [0.2.0-beta.1] — 2026-11-24 (`0.2.0b1`)

0MB 결정론 다중 테이블 ETL 엔진과 VectorDB 없는 원문 문서 인덱스·리트리벌 엔진을 추가한 두 번째 베타 릴리스.

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

### Changed

- **서비스 아키텍처 다이어그램 v0.2 갱신**: `assets/architecture.svg`·`assets/architecture.en.svg`를 3-트랙 구조(정형 ETL 0MB · 비정형 파싱 + 선택 OCR/VLM · 원문 문서 인덱스 0MB + 선택 CPU ONNX 리랭커)로 전면 개편

### Fixed

- **공개 실무 데이터(Northwind 11 CSV · Financial Sample XLSX · PDF/DOCX/PPTX/HWPX/HWP) 실전 검증 보완**:
  - 비인용 쉼표가 섞인 비표준 CSV에서 DuckDB 기본 dialect 스니퍼 실패 시 완화 파서(`strict_mode=false, null_padding=true, ignore_errors=true`) 자동 복구 및 경고 기록
  - 영문 우편번호(`EC1 4SD`) 등이 첫 데이터 행에 있을 때 2행 다중 헤더로 오인하던 문제 해결(2행 이상 헤더 판정에 텍스트 엄격 과반 `> 0.5` 요구)
  - 영문 Title-Case 단어(`January`, `High`) 및 공백 2단어 인명(`Maria Anders`)이 규칙 문자열로 오분해되던 현상 차단
  - 고유값 20개 초과 반복 문자열 FK 코드(`orders.customerID`)가 `free_text`로 빠지지 않고 `code_identifier`로 인식돼 테이블 간 FK가 정확히 탐지되도록 보완
  - camelCase 키(`orderID`, `productID`) 정규화 및 연결 테이블 복합 FK·약한 개체 `(FK, line_no)` 복합 PK 가산·비식별 칼럼 불필요 `UNIQUE INDEX` 제거
  - PDF/문서 추출 표의 통화 기호(`$`, `₩`, `€`, `£`, `¥`) 포함 금액 칼럼을 `DOUBLE`/`BIGINT`로 자동 추론해 수동 `CAST` 없이 DuckDB `SUM`/`AVG` SQL 집계 지원

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
