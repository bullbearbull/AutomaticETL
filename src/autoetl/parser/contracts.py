"""Auto Parser 계약 모델 — 문서 구조 분류·파싱 전략 라우팅·통합 IR.

PDF를 우선 깊게 구현하되, 다음 단계에서 PPTX·DOCX·HWPX 어댑터를 그대로 붙일 수
있도록 페이지/슬라이드 단위(`PageUnit`)와 요소별 IR을 형식 중립적으로 정의한다.
모든 모델은 `ContractModel`(extra="forbid")을 상속한다.
"""

from __future__ import annotations

import unicodedata
from enum import Enum

from pydantic import Field, NonNegativeInt, PositiveInt, field_validator

from autoetl.contracts.common import ContractModel

PARSER_CONTRACT_VERSION = "0.1.0"


class SourceFormat(str, Enum):
    """지원(또는 예정) 입력 문서 형식."""

    PDF = "pdf"
    PPTX = "pptx"
    DOCX = "docx"
    HWPX = "hwpx"
    HWP = "hwp"
    DOC = "doc"
    PPT = "ppt"
    XLSX = "xlsx"


class PageCategory(str, Enum):
    """페이지(또는 슬라이드) 단위의 구조 유형 분류."""

    DIGITAL_TEXT = "digital_text"
    MULTI_COLUMN = "multi_column"
    TABLE_HEAVY = "table_heavy"
    SCANNED_IMAGE = "scanned_image"
    HYBRID_TEXT_IMAGE = "hybrid_text_image"
    SLIDE_LIKE = "slide_like"
    VECTOR_DIAGRAM_HEAVY = "vector_diagram_heavy"
    BLANK = "blank"


class ElementKind(str, Enum):
    """문서 안에서 구분하는 자료(요소) 유형."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    FIGURE_IMAGE = "figure_image"
    VECTOR_DIAGRAM = "vector_diagram"
    SCANNED_PAGE = "scanned_page"
    CAPTION = "caption"
    HEADER_FOOTER = "header_footer"
    FORMULA_OR_CODE = "formula_or_code"


class ParseStrategy(str, Enum):
    """요소 또는 페이지에 배정하는 파싱 방법."""

    NATIVE_TEXT = "native_text"
    MULTI_COLUMN_LAYOUT = "multi_column_layout"
    RULED_TABLE = "ruled_table"
    HORIZONTAL_RULED_TABLE = "horizontal_ruled_table"
    BORDERLESS_TABLE = "borderless_table"
    EMBEDDED_IMAGE_OCR = "embedded_image_ocr"
    VLM_REGION = "vlm_region"
    VLM_FULL_PAGE = "vlm_full_page"
    SKIP_DECORATIVE = "skip_decorative"


class CropStatus(str, Enum):
    """시각 영역(OCR/VLM 대상)의 이미지 크롭 준비 상태."""

    SAVED = "saved"
    EMBEDDED_EXTRACTED = "embedded_extracted"
    SKIPPED = "skipped"
    RENDERER_UNAVAILABLE = "renderer_unavailable"
    FAILED = "failed"


class VisionStatus(str, Enum):
    """시각 영역(OCR/VLM)의 해석 처리 상태."""

    PENDING = "pending_vision"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class BBox(ContractModel):
    """좌상단 원점(top-left origin) 기준 경계 상자.

    좌표 단위는 포인트(pt, 1pt = 1/72 inch)이며, 선택적으로 페이지 크기 대비
    0~1 정규화 좌표를 함께 기록해 해상도/렌더러가 달라도 동일 위치를 재현한다.
    """

    x0: float
    top: float
    x1: float
    bottom: float
    norm_x0: float = Field(ge=0.0, le=1.0)
    norm_top: float = Field(ge=0.0, le=1.0)
    norm_x1: float = Field(ge=0.0, le=1.0)
    norm_bottom: float = Field(ge=0.0, le=1.0)

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def area(self) -> float:
        return self.width * self.height


class DocumentMetadata(ContractModel):
    """입력 문서 파일 수준 메타데이터."""

    source_path: str
    format: SourceFormat
    file_size_bytes: NonNegativeInt
    sha256: str
    page_count: PositiveInt
    title: str | None = None
    author: str | None = None
    creator: str | None = None
    producer: str | None = None
    has_outline: bool = False
    is_encrypted: bool = False

    @field_validator("source_path")
    @classmethod
    def _normalize_source_path_nfc(cls, value: str) -> str:
        # macOS 등에서 전달된 NFD 분해 한글 파일명을 표준 NFC로 정규화한다.
        return unicodedata.normalize("NFC", value)


class OutlineEntry(ContractModel):
    """문서 목차(북마크/아웃라인) 항목."""

    level: PositiveInt
    title: str
    page: PositiveInt | None = None


class PageMetrics(ContractModel):
    """페이지 구조 판정과 전략 라우팅의 근거가 되는 결정론 지표."""

    page: PositiveInt
    width_pt: float = Field(gt=0.0)
    height_pt: float = Field(gt=0.0)
    rotation: int = 0
    aspect_ratio: float = Field(gt=0.0, description="width / height. 1.15 이상이면 가로형")
    char_count: NonNegativeInt = 0
    usable_char_count: NonNegativeInt = 0
    usable_text_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    garbage_char_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    line_count: NonNegativeInt = 0
    column_count: NonNegativeInt = 1
    column_gutters_x: list[float] = Field(default_factory=list)
    median_font_size: float | None = None
    max_font_size: float | None = None
    image_count: NonNegativeInt = 0
    non_decorative_image_count: NonNegativeInt = 0
    image_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    vector_line_count: NonNegativeInt = 0
    vector_rect_count: NonNegativeInt = 0
    vector_curve_count: NonNegativeInt = 0
    non_table_vector_count: NonNegativeInt = 0
    table_count: NonNegativeInt = 0
    table_area_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    visual_region_count: NonNegativeInt = 0
    visual_area_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    header_footer_line_count: NonNegativeInt = 0


class RegionRoute(ContractModel):
    """페이지 안 개별 영역에 배정된 요소 유형·파싱 전략·결정 근거."""

    region_id: str
    element_kind: ElementKind
    strategy: ParseStrategy
    bbox: BBox
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    requires_vision: bool = False


class PageRouteDecision(ContractModel):
    """페이지 하나의 구조 분류 결과와 파싱 전략 계획."""

    page: PositiveInt
    category: PageCategory
    primary_strategy: ParseStrategy
    secondary_strategies: list[ParseStrategy] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    metrics: PageMetrics
    regions: list[RegionRoute] = Field(default_factory=list)
    requires_vision: bool = False
    warnings: list[str] = Field(default_factory=list)


class RoutingSummary(ContractModel):
    """문서 전체의 구조 분포와 파싱 전략 요약."""

    total_pages: PositiveInt
    analyzed_pages: PositiveInt
    category_counts: dict[str, int] = Field(default_factory=dict)
    strategy_counts: dict[str, int] = Field(default_factory=dict)
    table_count: NonNegativeInt = 0
    visual_region_count: NonNegativeInt = 0
    vision_required_pages: list[int] = Field(default_factory=list)
    multi_column_pages: list[int] = Field(default_factory=list)
    detected_body_font_size: float | None = None
    running_header_detected: bool = False
    running_footer_detected: bool = False


class RoutingPlan(ContractModel):
    """문서 구조 진단 및 페이지·요소별 파싱 전략 리포트 계약."""

    contract_version: str = PARSER_CONTRACT_VERSION
    document: DocumentMetadata
    outline: list[OutlineEntry] = Field(default_factory=list)
    summary: RoutingSummary
    pages: list[PageRouteDecision] = Field(default_factory=list)


class TableCell(ContractModel):
    """추출된 표의 개별 셀 (병합 셀의 rowspan/colspan 포함)."""

    row: NonNegativeInt
    col: NonNegativeInt
    row_span: PositiveInt = 1
    col_span: PositiveInt = 1
    text: str = ""
    is_header: bool = False
    bbox: BBox | None = None


class ExtractedTable(ContractModel):
    """구조를 보존해 추출한 표 하나."""

    table_id: str
    page: PositiveInt
    bbox: BBox
    strategy: ParseStrategy
    confidence: float = Field(ge=0.0, le=1.0)
    row_count: PositiveInt
    col_count: PositiveInt
    header_row_count: NonNegativeInt = 1
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    cells: list[TableCell] = Field(default_factory=list)
    has_merged_cells: bool = False
    caption: str | None = None
    continued_from_table_id: str | None = None
    reason: str = ""


class VisualRegion(ContractModel):
    """OCR 또는 VLM 처리가 필요한 시각 영역(스캔 페이지·그림·도식·차트)."""

    region_id: str
    page: PositiveInt
    element_kind: ElementKind
    recommended_strategy: ParseStrategy
    bbox: BBox
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    priority: str = Field(default="medium", pattern="^(high|medium|low)$")
    crop_status: CropStatus = CropStatus.SKIPPED
    crop_path: str | None = None
    image_width_px: int | None = None
    image_height_px: int | None = None
    embedded_text_hints: list[str] = Field(default_factory=list)
    caption: str | None = None
    vision_status: VisionStatus = VisionStatus.PENDING
    vision_adapter: str | None = None
    vision_markdown: str | None = None
    vision_text: str | None = None
    vision_metadata: dict[str, str] = Field(default_factory=dict)


class DocumentBlock(ContractModel):
    """읽기 순서대로 정렬된 통합 문서 블록(IR 단위)."""

    block_id: str
    page: PositiveInt
    reading_order: NonNegativeInt
    column_index: NonNegativeInt = 0
    kind: ElementKind
    strategy: ParseStrategy
    bbox: BBox
    text: str = ""
    heading_level: int | None = Field(default=None, ge=1, le=6)
    font_size: float | None = None
    is_bold: bool = False
    table_id: str | None = None
    visual_region_id: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ParsedPage(ContractModel):
    """페이지 하나의 완전한 파싱 결과."""

    page: PositiveInt
    route: PageRouteDecision
    blocks: list[DocumentBlock] = Field(default_factory=list)
    header_footer_blocks: list[DocumentBlock] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    visual_regions: list[VisualRegion] = Field(default_factory=list)


class ParsedDocument(ContractModel):
    """Auto Parser 최종 통합 산출물(JSON IR + 라우팅 계획 + 표 + 시각 영역)."""

    contract_version: str = PARSER_CONTRACT_VERSION
    document: DocumentMetadata
    routing_plan: RoutingPlan
    pages: list[ParsedPage] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    visual_regions: list[VisualRegion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DocumentChunk(ContractModel):
    """RAG·검색·LLM 컨텍스트 투입용 구조화 문서 청크 단위."""

    chunk_id: str
    chunk_index: NonNegativeInt
    source_path: str
    format: SourceFormat
    sha256: str
    page_start: PositiveInt
    page_end: PositiveInt
    section_path: list[str] = Field(default_factory=list)
    element_kinds: list[ElementKind] = Field(default_factory=list)
    text: str
    char_count: NonNegativeInt
    estimated_tokens: NonNegativeInt
    table_ids: list[str] = Field(default_factory=list)
    visual_region_ids: list[str] = Field(default_factory=list)
