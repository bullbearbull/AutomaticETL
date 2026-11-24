# parser — Auto Parser (문서 구조 분류·전략 라우팅·통합 IR·RAG 청킹·배치·로컬 UI)

비정형·오피스 문서 **8대 포맷**(모던 **PDF·PPTX·DOCX·HWPX·XLSX** + 구형 바이너리 OLE2 **HWP 5.0·DOC·PPT**)을 입력받아
페이지·슬라이드·시트·섹션·요소별 구조를 결정론적으로 분류하고 자료 유형별 파싱 전략으로 추출하는 독립 패키지다.
기존 CSV 정형 엔진(`core/`)과 독립적으로 동작하며, 추가 핵심 런타임 의존성 0개로 동작한다.

## 구성

- `contracts.py` — 닫힌 계약(`ContractModel`) 정의: `SourceFormat`(8종), `RoutingPlan`, `PageRouteDecision`,
  `PageMetrics`, `ExtractedTable`, `VisualRegion`, `DocumentBlock`, `DocumentChunk`, `ParsedDocument`.
- `adapters.py` — 확장 경계:
  - `DocumentFormatAdapter` — 형식별 파서(**PDF·PPTX·DOCX·HWPX·XLSX·HWP·DOC·PPT 8종 기본 등록**)
  - `PageLayoutDetector` — 스캔/이미지 페이지를 제목·본문·표·차트·수식으로 분할하는 객체 탐지 인터페이스
  - `VisionParserAdapter` — 스캔·그림·도식 영역용 OCR/VLM 플러그인 인터페이스
  - `PageRenderer` — 벡터 도식 등 래스터 크롭용 선택 렌더러(`pypdfium2` → `pdftoppm` → 내장 이미지 폴백)
- `chunker.py` — RAG/LLM 구조화 청킹 엔진(`ChunkOptions`, `chunk_document`, `render_chunks_jsonl`, `write_chunks_jsonl`):
  - 제목 계층 브레드크럼(`section_path`)·페이지 범위(`page_start`~`page_end`)·요소 유형(`element_kinds`)·토큰 추정치(`estimated_tokens`) 보존
  - 표 행 무결성 분할(대형 표 분할 시 헤더 자동 반복) 및 완료된 VLM/OCR 시각 해석 텍스트 통합
- `batch.py` — 폴더 일괄 파싱 파이프라인(`discover_batch_files`, `parse_batch`, `BatchManifest`, `render_batch_summary_markdown`):
  - Office 임시 파일(`~$*`, `.~lock.*`) 자동 제외, 파일 단위 오류 격리(본문·스택 비노출), 충돌 없는 출력 경로(`stem__ext`)
  - SHA-256 기반 증분 재개(`--resume`), 병렬 워커(`--workers`), 통합 `batch_summary.md`·`batch_manifest.json`·`all_chunks.jsonl` 생성
- `ui/` — 무의존성 표준 라이브러리(`ThreadingHTTPServer`) 기반 로컬 웹 UI(`autoetl ui`):
  - 루프백(`127.0.0.1`) 전용 바인딩 기본 강제, 오프라인 단일 페이지 앱(`app.html`), 5개 결과 탭(개요·Markdown·추출 표·RAG 청크·시각 영역), ZIP 번들 다운로드
- `image_io.py` — 외부 Pillow 없이 동작하는 순수 Python PNG 인코더·디코더·부분 크롭기 및 PDF 내장 이미지 추출기.
- `layout_detector.py` — 스캔 페이지 레이아웃 객체 탐지기 구현체(추가 핵심 의존성 0개):
  - `HeuristicRasterLayoutDetector` — 순수 Python PNG 픽셀 투영·격자선·색채/밀도 분석(즉시 실행 가능)
  - `OnnxDocLayoutDetector` — 선택 의존성 `onnxruntime`+`numpy` 기반 Apache-2.0 `PP-DocLayoutV2`/`V3`·`PP-DocLayout_plus-L`·`DocLayNet` ONNX 추론
  - `HttpLayoutDetector` — 로컬/사내 `POST /v1/layout/detect` 서비스 연동(`validate_vision_endpoint` TrustZone 보안 경계 적용)
- `exporters.py` — 통합 Markdown(`document.md`), RAG 청크(`chunks.jsonl`), 구조·전략 리포트(`routing_report.md`/`.json`),
  표 CSV/JSON(`tables/`), 통합 IR JSON(`parsed.json`) 내보내기.
- `vision.py` — 무의존성 OCR/VLM 어댑터 구현체:
  - `TesseractOcrAdapter` — 로컬 `tesseract` 바이너리(stdin→stdout TSV, 임시 파일 0건)
  - `OpenAICompatibleVlmAdapter` — Ollama(`/v1`)·llama-server·vLLM·LM Studio·OpenAI 호환 Vision API
  - `OllamaVisionAdapter` — Ollama 네이티브 `/api/chat` Vision API
  - `RoutedVisionAdapter` — `ParseStrategy`별 OCR/VLM 자동 분배·교차검증 힌트·OCR 폴백
  - `VisionPromptPreset` — `auto`(기본)·`generic`·`glm-ocr`·`paddleocr-vl`·`deepseek-ocr` 짧은 태스크 프롬프트 프리셋(모델명 자동 감지)
  - `validate_vision_endpoint` — `autoetl.security.levels.trust_zone` 기반 루프백·등록 사내 기본 허용 / 외부 기본 차단
- `pipeline.py` — 형식 어댑터 레지스트리 및 `inspect_document`, `parse_document`, `parse_and_export`, `apply_vision_to_document`.
- `pdf/` — PDF 심층 구현(`geometry.py`, `extractor.py`, `tables.py`, `visuals.py`, `layout.py`, `router.py`, `adapter.py`).
- `pptx/` — PPTX(OOXML) 무의존성 심층 구현(`ooxml.py`, `elements.py`, `router.py`, `adapter.py`, `renderer.py`):
  - 슬라이드 레이아웃·마스터 상속, 중첩 그룹 도형(`grpSp`) 좌표 변환, 최대 공백 우선 재귀 XY-Cut 읽기 순서
  - 병합 셀(`gridSpan`/`rowSpan`) 네이티브 표·희소 병합 표 압축·네이티브 차트(`chart*.xml`)·Office 2016+ 확장 차트(`chartEx*.xml`, 폭포·트리맵·선버스트·깔때기 등)·내장 Excel(`.xlsx`) OLE 시트 직접 테이블화
  - SmartArt 계층 트리·연결선(`cxnSp`) 도식 흐름·발표자 노트·선택 슬라이드 주석(`--include-comments`)·VML/SVG 미리보기 추출 및 선택 `LibreOfficePptxRenderer`(`--pptx-renderer libreoffice`)·구형 `.ppt` 변환(`convert_ppt_to_pptx`)
- `docx/` — DOCX(WordprocessingML OOXML) 무의존성 심층 구현(`ooxml.py`, `elements.py`, `router.py`, `adapter.py`):
  - `word/styles.xml` `basedOn` 상속 체인·`outlineLvl`·코드 스타일, `word/numbering.xml` 다단계 번호/글머리표 목록 해석
  - 변경 내용 추적(`w:ins` 보존·`w:del`/`w:moveFrom` 제외), 콘텐츠 컨트롤(`w:sdt`)·하이퍼링크·`mc:AlternateContent` 중복 제거(`wps:txbx` Choice vs `v:textbox` Fallback)
  - 병합 셀(`gridSpan`·`hMerge`·`vMerge` restart/continue)·중첩 표·테두리 유형(`RULED_TABLE`/`HORIZONTAL_RULED_TABLE`/`BORDERLESS_TABLE`)·희소 패딩 열 압축
  - Office Math(`m:oMath`/`m:oMathPara` 분수·위첨자·아래첨자·근호·적분/합·행렬) 선형 수식 변환, 각주(`word/footnotes.xml`)·미주(`word/endnotes.xml`)·검토 주석(`word/comments.xml`, `--include-comments`)·머리글/바닥글 분리
  - 내장 이미지·네이티브 차트(`c:chart`·`cx:chart`)·SmartArt·내장 Excel(`.xlsx`) OLE 추출, 페이지 나누기/섹션/가상 높이 페이지 분할 및 전체 스캔 페이지 `PageLayoutDetector` 연동
- `hwpx/` — HWPX(KS X 6101 OWPML 한글 개방형 문서) 무의존성 심층 구현(`owpml.py`, `elements.py`, `router.py`, `adapter.py`):
  - `META-INF/container.xml`·`Contents/content.hpf` 매니페스트/스파인/메타데이터, `Contents/header.xml` 글꼴·글자모양(`charPr`)·문단모양(`paraPr` 개요/번호/글머리표)·스타일(`style`)·테두리(`borderFill`) 카탈로그
  - `<hp:switch>`(`<hp:case>` vs `<hp:default>`) 중복 제거, `<hp:ctrl>` 내 머리글(`header`)·바닥글(`footer`)·각주(`footNote`)·미주(`endNote`)·숨은주석(`hiddenComment`) 본문 혼입 방지 및 분리 추출
  - `<hp:tbl>` 명시적 격자 좌표(`cellAddr`)·가로세로 병합(`cellSpan`)·테두리 전략(`RULED_TABLE`/`HORIZONTAL_RULED_TABLE`/`BORDERLESS_TABLE`)·내장 캡션(`caption`)·중첩 표 추출
  - 한컴 수식 스크립트(`<hp:equation><hp:script>`) 선형 수식 정규화, `BinData/*` 내장 이미지·OOXML 차트(`Chart/*.xml`)·도형 컨테이너(`container`/`drawText`)·다단(`colPr`)·페이지 나누기(`pageBreak`/`linesegarray`) 및 전체 스캔 페이지 `PageLayoutDetector` 연동
- `xlsx/` — XLSX(SpreadsheetML OOXML) 무의존성 심층 구현(`ooxml.py`, `elements.py`, `adapter.py`):
  - `ET.iterparse` 스트리밍 시트 파싱, `sharedStrings.xml` 음성 힌트(`<rPh>`) 제거, `styles.xml` 날짜(1900/1904)·백분율·천단위 서식 복원
  - 병합 셀(`mergeCells`) 확장 및 다단 헤더 평탄화, 빈 행·빈 열 기준 다중 데이터 영역 분할, 시트별 메모/주석(`--include-comments`)
  - DrawingML 차트(`c:chart`·`cx:chart`) 표 변환 및 내장 이미지 크롭 추출, OLE2 암호화 `.xlsx` 안전 차단
- `legacy/` — 구형 바이너리 OLE2 문서(`.hwp`, `.doc`, `.ppt`) 무의존성 네이티브 + 하이브리드 구현(`cfb.py`, `converter.py`, `hwp.py`, `doc.py`, `ppt.py`):
  - `cfb.py` — 순수 Python MS-CFB(OLE2) v3/v4 리더(`CfbFile`, FAT·DIFAT·MiniFAT·디렉터리 트리), `\x05SummaryInformation`/`\x05HwpSummaryInformation` 속성셋 파서, OfficeArt BLIP(PNG/JPEG/BMP/EMF/WMF) 스캐너
  - `converter.py` — 시스템에 LibreOffice(`soffice`/`libreoffice`)가 설치된 환경에서 `.doc`→`.docx`, `.ppt`→`.pptx` 고충실도 승격을 지원하는 선택 변환기(`convert_legacy_office_document`)
  - `hwp.py` — 순수 Python HWP 5.0(`.hwp`) 네이티브 파서(`HwpDocumentAdapter`): `FileHeader` 암호화·DRM·배포용(`ViewText`) 차단, `DocInfo` 스타일·글꼴·글자/문단모양·번호·테두리·BinData 캐시, 섹션 스트리밍 레코드 트리(`BodyText/Section*`), 병합 표·캡션·수식(`eqed`)·덧말(`tdut`)·글자겹침(`tcps`)·하이퍼링크(`%hlk`)·각주/미주/숨은설명(`tcmt`)·머리글/바닥글·그림(`BinData/*`)·OLE(`$ole`/`0x54`) 및 스캔 페이지 `PageLayoutDetector` 연동
  - `doc.py` — MS Word 97–2003(`.doc`) 하이브리드 어댑터(`DocDocumentAdapter`): Tier-1 LibreOffice `.docx` 승격(설치 시) + Tier-2 순수 Python MS-DOC 폴백(`WordDocument` FIB 암호화 차단, `0Table`/`1Table` `Clx`/`PlcPcd` 피스 테이블 UTF-16LE/CP1252 디코딩, `\x07` 표 복원, 필드/하이퍼링크 해석, 머리글/바닥글/각주/미주/주석 서브도큐먼트, `Data`/Escher BLIP 이미지 추출)
  - `ppt.py` — MS PowerPoint 97–2003(`.ppt`) 하이브리드 어댑터(`PptDocumentAdapter`): Tier-1 LibreOffice `.pptx` 승격(설치 시) + Tier-2 순수 Python MS-PPT 폴백(`Current User` 암호화 차단, `UserEditAtom`/`PersistDirectoryAtom` 증분 저장 복원, `SlideListWithText`·`Slide`·`Notes`·`HeadersFootersContainer`(`0x0FD9`)·Escher 표(`fIsTable`)·`Pictures` BLIP 이미지 추출)

## 빠른 실행

```bash
# 0. 실행 환경 및 선택 백엔드 진단 / 로컬 웹 UI 실행
uv run autoetl doctor
uv run autoetl ui --port 8765

# 1. 구조 분류 및 페이지·슬라이드·시트별 파싱 전략 리포트만 확인 (8대 포맷 공통)
uv run autoetl parse report.pdf --plan-only
uv run autoetl parse deck.pptx --plan-only
uv run autoetl parse document.docx --plan-only
uv run autoetl parse proposal.hwpx --plan-only
uv run autoetl parse workbook.xlsx --plan-only
uv run autoetl parse notice.hwp --plan-only
uv run autoetl parse legacy.doc --plan-only
uv run autoetl parse legacy.ppt --plan-only

# 2. 단일 문서 전체 파싱 후 번들(parsed.json, document.md, chunks.jsonl, routing_report.*, tables/, crops/) 저장
uv run autoetl parse workbook.xlsx --include-comments --out ./parsed_xlsx
uv run autoetl parse notice.hwp --include-comments --out ./parsed_hwp
uv run autoetl parse proposal.hwpx --include-comments --out ./parsed_hwpx
uv run autoetl parse report.pdf --chunk-max-chars 1200 --chunk-overlap 150 --out ./parsed_pdf

# 3. stdout으로 RAG 청크(JSONL) 또는 Markdown 바로 출력
uv run autoetl parse report.pdf --format chunks
uv run autoetl parse workbook.xlsx --format markdown

# 4. 폴더 일괄 파싱(재귀 탐색·병렬 워커·SHA-256 증분 재개·통합 all_chunks.jsonl + batch_summary.md)
uv run autoetl parse ./documents --out ./parsed_batch --workers 4 --resume
uv run autoetl parse ./documents --out ./parsed_batch --include-ext .pdf,.hwp,.xlsx

# 5. 스캔 PDF / 스크린샷 슬라이드: 무의존성 휴리스틱 레이아웃 분할 + 로컬 Tesseract OCR
uv run autoetl parse scanned.pdf --layout-detector heuristic --vision tesseract --out ./parsed_out

# 6. 스캔 PDF 2단계 권장 구성: Apache-2.0 PP-DocLayoutV2(ONNX) + PaddleOCR-VL / GLM-OCR
uv run autoetl parse scanned.pdf \
  --layout-detector onnx --layout-model-path ./models/PP-DocLayoutV2.onnx \
  --vision openai-compat --vision-model PaddleOCR-VL-1.6 \
  --vision-base-url http://127.0.0.1:8080/v1 --out ./parsed_out

# 7. 로컬 VLM(Ollama / llama-server / vLLM) + OCR 하이브리드 라우팅
uv run autoetl parse deck.pptx --vision hybrid --vision-model qwen2.5vl:7b \
  --vision-base-url http://127.0.0.1:11434/v1 --out ./parsed_out

# 8. 외부 OpenAI 호환 API 사용 시 명시적 허용(--allow-external-vision)과 환경변수 키 지정 필요
uv run autoetl parse report.pdf --vision openai-compat --vision-model gpt-4o \
  --vision-base-url https://api.openai.com/v1 --vision-api-key-env OPENAI_API_KEY \
  --allow-external-vision --out ./parsed_out
```

## 확장 방법

- **새 형식 어댑터 추가**: `DocumentFormatAdapter`를 구현해
  `autoetl.parser.register_adapter(...)`로 등록하면 계약·리포트·CLI·배치·웹 UI·내보내기가 그대로 동작한다.
- **커스텀 레이아웃 객체 탐지기 연결**: `PageLayoutDetector`를 구현해
  `parse_document(..., layout_detector=...)`로 넘기면 스캔 페이지·스크린샷 슬라이드가 자동 세분화·크롭되어
  각 영역의 `ElementKind`에 맞는 OCR/VLM 프롬프트로 분기된다.
- **커스텀 OCR/VLM 연결**: `VisionParserAdapter`를 구현해 `parse_document(..., vision_adapter=...)`
  또는 사후 적용 `apply_vision_to_document(doc, adapter)`로 넘기면 IR·Markdown·RAG 청크에 자동 반영된다.
