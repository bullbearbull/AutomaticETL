# Changelog

이 문서는 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 따릅니다.

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
