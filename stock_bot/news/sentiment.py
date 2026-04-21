"""뉴스 제목·요약 감성 분석.

1) 기본: 한국어 호재/악재 키워드 매칭 → -1 ~ +1 점수
2) 선택: ANTHROPIC_API_KEY 가 있으면 Claude 로 헤드라인을 의미적으로 분석
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

from loguru import logger

POSITIVE_TERMS = [
    "호재", "급등", "상승", "흑자", "매수", "추천", "상향", "기대", "성장",
    "호실적", "신고가", "돌파", "사상최대", "최대실적", "흥행", "수혜", "호조",
    "낙관", "반등", "급등세", "강세", "목표가상향", "실적개선", "수주", "계약체결",
    "승인", "통과", "출시", "확대", "증가", "개선", "수익", "배당", "자사주매입",
]

NEGATIVE_TERMS = [
    "악재", "급락", "하락", "적자", "매도", "하향", "부진", "감익", "손실",
    "위기", "리스크", "의혹", "수사", "횡령", "배임", "감자", "유상증자",
    "파산", "법정관리", "거래정지", "상장폐지", "관리종목", "소송", "경고",
    "약세", "매도세", "목표가하향", "실적악화", "쇼크", "어닝쇼크", "부도",
    "급락세", "폭락", "디스카운트", "지연", "연기", "취소", "철회",
]

# 강도 조절 (강한 긍·부정)
STRONG_POS = {"사상최대", "어닝서프라이즈", "급등", "신고가", "돌파"}
STRONG_NEG = {"어닝쇼크", "상장폐지", "거래정지", "폭락", "파산", "감자"}


@dataclass
class SentimentResult:
    score: float  # -1 ~ +1
    positives: list[str]
    negatives: list[str]
    method: str  # "keyword" | "llm"


def _count_terms(text: str, terms: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for t in terms:
        if t in text:
            hits.append(t)
    return hits


def score_sentiment_keyword(text: str) -> SentimentResult:
    """키워드 개수 기반 점수. (pos - neg) / (pos + neg + 1)."""
    clean = re.sub(r"\s+", " ", text or "")
    pos = _count_terms(clean, POSITIVE_TERMS)
    neg = _count_terms(clean, NEGATIVE_TERMS)
    weight = 0.0
    for p in pos:
        weight += 2.0 if p in STRONG_POS else 1.0
    for n in neg:
        weight -= 2.0 if n in STRONG_NEG else 1.0
    total = sum(2.0 if x in STRONG_POS | STRONG_NEG else 1.0 for x in pos + neg) + 1.0
    score = max(-1.0, min(1.0, weight / total))
    return SentimentResult(score=score, positives=pos, negatives=neg, method="keyword")


def score_sentiment_llm(text: str) -> SentimentResult | None:
    """Claude API 로 의미 기반 점수. 키 없으면 None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        return None

    client = Anthropic(api_key=api_key)
    prompt = (
        "다음 한국 주식 뉴스 헤드라인의 투자 심리를 평가해줘. "
        "주가에 긍정적이면 +1, 부정적이면 -1, 중립이면 0 사이의 점수만 "
        "소수점 첫째자리까지 숫자로 출력. 설명 금지.\n\n"
        f"헤드라인: {text}"
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        match = re.search(r"-?\d+(\.\d+)?", raw)
        if not match:
            return None
        score = max(-1.0, min(1.0, float(match.group(0))))
        return SentimentResult(score=score, positives=[], negatives=[], method="llm")
    except Exception as exc:
        logger.warning("LLM sentiment failed: {}", exc)
        return None


def score_sentiment(text: str, prefer_llm: bool = False) -> SentimentResult:
    if prefer_llm:
        res = score_sentiment_llm(text)
        if res is not None:
            return res
    return score_sentiment_keyword(text)


def summarize_symbol_sentiment(results: list[SentimentResult]) -> float:
    """여러 기사의 점수를 평균. 최근일수록 가중은 호출 측 책임."""
    if not results:
        return 0.0
    return sum(r.score for r in results) / len(results)
