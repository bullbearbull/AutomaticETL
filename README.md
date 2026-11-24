# AutoETL

[![CI](https://github.com/bullbearbull/AutomaticETL/actions/workflows/ci.yml/badge.svg)](https://github.com/bullbearbull/AutomaticETL/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)

[English](README.en.md) · [파서 상세 문서](src/autoetl/parser/README.md) · [변경 이력](CHANGELOG.md)

로컬에서 바로 돌리는 문서 파서 겸 ETL 도구입니다.  
PDF, Office 문서, 한글(HWP/HWPX) 파일을 외부 API나 무거운 런타임 없이 Markdown, 표(CSV/JSON), RAG용 청크(`chunks.jsonl`)로 꺼낼 수 있습니다.

> 현재 **`v0.1.0-beta.1`** 베타 상태입니다. 아직 PyPI에는 올리지 않았고 Git으로 설치해 쓸 수 있습니다.
> - **문서 파서 (`parse`, `ui`, `doctor`)**: 8개 포맷 파싱, 청킹, 폴더 일괄 처리, 로컬 웹 UI까지 바로 사용 가능
> - **정형 ETL (`analyze`)**: CSV `--no-llm` 분석 프로토타입 단계 (LLM 하네스·PostgreSQL DDL 연동 작업 중)

---

## 설치

Python 3.10 이상(Windows, macOS, Linux)에서 동작합니다.

```bash
pip install git+https://github.com/bullbearbull/AutomaticETL.git
```

개발 환경으로 받으려면 [uv](https://docs.astral.sh/uv/)를 씁니다.

```bash
git clone https://github.com/bullbearbull/AutomaticETL.git
cd AutomaticETL
uv sync
```

---

## 사용법

### 파일 하나 파싱하기

```bash
autoetl parse report.pdf --out ./out
```

실행하면 아래 결과물이 `./out` 폴더에 쌓입니다.

```text
out/
├── document.md         # 본문·표·각주·수식을 합친 Markdown
├── chunks.jsonl        # 제목 경로·페이지·표 헤더를 유지한 RAG 청크
├── parsed.json         # 전체 구조 IR (Pydantic 스키마)
├── routing_report.md   # 페이지·시트별 구조 판정과 파싱 전략 리포트
├── routing_report.json
├── tables/             # 추출된 표별 .csv / .json
└── crops/              # 스캔·그림·도식 영역 이미지 크롭
```

출력 파일 없이 터미널에서 바로 파이프로 넘길 수도 있습니다.

```bash
autoetl parse report.pdf --plan-only       # 구조 진단 리포트만 확인
autoetl parse report.pdf --format markdown # Markdown을 stdout으로 출력
autoetl parse report.pdf --format chunks   # RAG 청크(JSONL)를 stdout으로 출력
```

### 폴더 통째로 처리하기

입력 경로에 폴더를 넘기면 지원되는 문서를 재귀적으로 찾아 일괄 처리합니다.

```bash
autoetl parse ./docs --out ./batch_out --workers 4 --resume
```

- 문서 하나가 깨지거나 암호화되어 있어도 나머지 파일은 계속 진행합니다.
- `--resume`을 켜면 SHA-256 기준으로 이미 끝난 파일은 건너뜁니다.
- 끝나면 요약 리포트(`batch_summary.md`), 실행 기록(`batch_manifest.json`), 전체 문서 청크를 합친 `all_chunks.jsonl`이 함께 만들어집니다.

### 브라우저에서 바로 써보기

```bash
autoetl ui
```

별도 프런트엔드 빌드나 외부 서버 없이 `http://127.0.0.1:8765`에 로컬 웹 UI가 뜹니다.  
파일을 끌어다 놓으면 Markdown, 표(CSV/Markdown 복사), RAG 청크 검색, 크롭 이미지를 탭별로 확인하고 전체 번들을 ZIP으로 받을 수 있습니다. 기본적으로 루프백(`127.0.0.1`)에만 열립니다.

### Python에서 불러 쓰기

```python
import autoetl

doc = autoetl.parse("notice.hwp")
chunks = autoetl.chunk_document(doc, options=autoetl.ChunkOptions(max_chars=1500))
manifest = autoetl.export_bundle(doc, "./out_hwp")

# 폴더 일괄 처리
batch = autoetl.parse_batch("./docs", "./batch_out", workers=4, resume=True)
```

### 환경 점검

```bash
autoetl doctor
```

현재 설치된 Python 환경에서 8개 기본 포맷과 선택 백엔드(LibreOffice, PDF 렌더러, ONNX, Tesseract) 중 무엇을 쓸 수 있는지 표로 보여줍니다.

---

## 지원 포맷

핵심 파서 8종은 PDF(`pypdf`)를 빼면 전부 Python 표준 라이브러리로만 구현했습니다.

| 포맷 | 기본 처리 방식 | 참고 |
| --- | --- | --- |
| `.pdf` | `pypdf` + 자체 기하/표/이미지 추출기 | 다단 읽기 순서, 격자·무선 표, 스캔 페이지 크롭 |
| `.docx` | 내장 OOXML 파서 | 스타일·다단계 번호·변경추적·수식(OMML)·표·각주·주석 |
| `.pptx` | 내장 OOXML 파서 | 슬라이드 레이아웃·그룹 도형·표·차트·내장 엑셀·발표자 노트 |
| `.xlsx` | 내장 OOXML 스트리밍 파서 | 시트별 표·병합 셀·다단 헤더·날짜/숫자 서식·주석·차트 |
| `.hwpx` | 내장 OWPML 파서 | 한글 개요·병합 표·수식·머리글/바닥글·각주·숨은주석 |
| `.hwp` | 내장 HWP 5.0(OLE2) 파서 | 본문·병합 표·수식·각주·BinData 이미지 (암호화·배포용은 차단) |
| `.doc`, `.ppt` | 내장 OLE2 파서 (+ LibreOffice 자동 승격) | `soffice`가 PATH에 있으면 고충실도 변환, 없으면 내장 파서로 폴백 |

주석·숨은설명이 필요하면 `--include-comments`를 붙이면 됩니다.  
암호화되거나 DRM·배포용 제한이 걸린 문서는 우회하지 않고 오류 메시지로 알려줍니다.

---

## 스캔 문서 OCR · VLM 연동 (선택)

디지털 텍스트는 기본 설정만으로 로컬에서 추출됩니다. 스캔 PDF나 이미지 위주 슬라이드만 필요할 때 선택 백엔드를 붙여서 씁니다.

```bash
# 로컬 레이아웃 분할 + Tesseract OCR
autoetl parse scanned.pdf --layout-detector heuristic --vision tesseract --out ./out

# 로컬 Ollama / OpenAI 호환 서버(vLLM, llama-server 등) 연동
autoetl parse scanned.pdf \
  --vision openai-compat --vision-model PaddleOCR-VL-1.6 \
  --vision-base-url http://127.0.0.1:8080/v1 --out ./out
```

- 외부 API 엔드포인트로 이미지를 보내는 동작은 기본 차단되어 있습니다. 외부 서버를 쓰려면 `--allow-external-vision`과 API 키 환경변수 이름(`--vision-api-key-env`)을 직접 지정해야 합니다.
- 로그와 오류 메시지에는 문서 본문이나 셀 값을 남기지 않습니다.

---

## 정형 CSV 분석 (작업 중)

CSV 프로파일링과 스키마 설계 엔진은 아직 v0 단계입니다. 지금은 `--no-llm` 경로만 열려 있습니다.

```bash
autoetl analyze sample.csv --no-llm --out report.json
```

통계, 타입 추론, 관계 분석, PostgreSQL DDL 생성은 코드가 결정론적으로 계산하고, LLM은 정해진 판단 지점에서만 돕는 구조로 확장하고 있습니다.

---

## 개발

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python tools/license_check.py
```

CI는 Windows, macOS, Linux × Python 3.10~3.13에서 테스트, 린트, strict mypy, 런타임 의존성 라이선스 검사를 돌립니다.  
기여 방법은 [CONTRIBUTING.md](CONTRIBUTING.md)를 참고해 주세요.

## 라이선스

[Apache-2.0](LICENSE)
