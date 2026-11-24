"""무의존성(0MB) 한국어 2-gram + 공백 토큰 혼합 FTS5 토크나이저.

별도 형태소 분석기(MeCab·Kiwi·JVM) 없이도 한국어 복합명사(예: '개인정보보호법' → '개인정보',
'보호법')와 조사 변화('매출액은'/'매출액이' → '매출액'), 영문·숫자 식별 코드('ORD-2026-KR')를
SQLite FTS5 BM25로 빠르고 결정론적으로 검색할 수 있게 한다.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+(?:[-_./][0-9A-Za-z가-힣]+)*")
_HANGUL_RUN_RE = re.compile(r"[가-힣]{2,}")
_STOPWORDS = frozenset(
    {
        "의",
        "가",
        "이",
        "은",
        "는",
        "을",
        "를",
        "에",
        "에서",
        "으로",
        "로",
        "과",
        "와",
        "및",
        "등",
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "are",
        "was",
    }
)


def extract_index_tokens(text: str) -> list[str]:
    """원문 텍스트를 FTS5 색인용 [원본 토큰 + 한글 2-gram] 목록으로 변환한다."""
    if not text:
        return []
    tokens: list[str] = []
    for match in _WORD_RE.finditer(text.lower()):
        raw = match.group(0)
        if len(raw) == 1 and raw in _STOPWORDS:
            continue
        tokens.append(raw)
        # 하이픈·점·슬래시로 이어진 코드/식별자는 하위 조각도 함께 색인
        if any(sep in raw for sep in ("-", "_", ".", "/")):
            for part in re.split(r"[-_./]+", raw):
                if len(part) >= 2 and part != raw:
                    tokens.append(part)
        # 한글 연속 구간은 2-gram을 함께 색인해 조사/복합명사 부분 매칭 지원
        for h_run in _HANGUL_RUN_RE.findall(raw):
            n = len(h_run)
            if n >= 2:
                for i in range(n - 1):
                    tokens.append(h_run[i : i + 2])
    return tokens


def index_text_for_fts(text: str) -> str:
    """FTS5 칼럼에 저장할 공백 구분 색인 문자열을 생성한다."""
    return " ".join(extract_index_tokens(text))


def extract_query_terms(query: str) -> list[str]:
    """사용자 질의에서 중복 없는 핵심 검색 토큰(원본 단어 + 한글 2-gram)을 추출한다."""
    if not query.strip():
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for tok in extract_index_tokens(query):
        if tok in _STOPWORDS:
            continue
        if tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    return ordered


def build_fts5_match_expr(terms: list[str], *, max_terms: int = 32) -> str:
    """FTS5 구문 오류를 원천 차단한 안전한 OR MATCH 표현식을 조립한다."""
    if not terms:
        return ""
    safe_parts: list[str] = []
    for term in terms[:max_terms]:
        cleaned = term.replace('"', "").strip()
        if cleaned:
            safe_parts.append(f'"{cleaned}"')
    return " OR ".join(safe_parts)


def compute_outline_overlap(query_terms: list[str], title: str, section_path: list[str]) -> float:
    """질의 토큰과 섹션 제목·목차 경로 간 결정론적 일치 보너스(0.0~1.0)를 계산한다."""
    if not query_terms:
        return 0.0
    heading_tokens = set(extract_index_tokens(f"{title} {' '.join(section_path)}"))
    if not heading_tokens:
        return 0.0
    hits = sum(1 for t in query_terms if t in heading_tokens)
    return min(1.0, hits / len(query_terms))


__all__ = [
    "build_fts5_match_expr",
    "compute_outline_overlap",
    "extract_index_tokens",
    "extract_query_terms",
    "index_text_for_fts",
]
