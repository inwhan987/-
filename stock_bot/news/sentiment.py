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
    # 추가
    "역대최고", "사상최고", "최고가경신", "경신", "폭주", "질주", "랠리",
    "훈풍", "회복", "턴어라운드", "순항", "약진", "부양", "호황", "호평",
    "어닝서프라이즈", "깜짝실적", "최대매출", "흑자전환", "신기록",
]

NEGATIVE_TERMS = [
    "악재", "급락", "하락", "적자", "매도", "하향", "부진", "감익", "손실",
    "위기", "리스크", "의혹", "수사", "횡령", "배임", "감자", "유상증자",
    "파산", "법정관리", "거래정지", "상장폐지", "관리종목", "소송", "경고",
    "약세", "매도세", "목표가하향", "실적악화", "쇼크", "어닝쇼크", "부도",
    "급락세", "폭락", "디스카운트", "지연", "연기", "취소", "철회",
    # 시황·주가 추가
    "타격", "압박", "충격", "위축", "둔화", "후퇴", "적자전환", "경고등",
    "먹구름", "빨간불", "추락", "붕괴", "폐업", "제재", "과징금",
    "리콜", "불확실성", "우려확산", "매출감소", "수익성악화", "마진축소",
    "실망", "실적쇼크", "가이던스하향", "역성장",
    # 노동·거버넌스 리스크
    "파업", "총파업", "파업예고", "노조반발", "직장폐쇄", "태업",
    "오너리스크", "경영권분쟁", "내부고발", "해임", "사퇴압박",
    # 규제·법적 리스크
    "기소", "구속", "압수수색", "공정위조사", "세무조사", "특검",
    "담합", "과태료", "영업정지", "인허가취소", "집단소송",
    # 외부 악재
    "화재", "사고", "중단", "유출", "해킹", "개인정보유출", "품질문제",
    "결함", "무더기매도", "외국인매도", "기관매도",
]

# 강도 조절 (강한 긍·부정)
STRONG_POS = {
    "사상최대", "사상최고", "역대최고", "어닝서프라이즈", "깜짝실적",
    "급등", "신고가", "돌파", "폭주", "흑자전환",
}
STRONG_NEG = {
    "어닝쇼크", "상장폐지", "거래정지", "폭락", "파산", "감자",
    "적자전환", "붕괴", "추락", "리콜", "총파업", "영업정지",
    "구속", "압수수색", "실적쇼크", "해킹", "개인정보유출",
}


def _normalize(text: str) -> str:
    """공백·구두점 제거해 '사상 최고' → '사상최고' 같은 변형을 매칭 가능하게."""
    return re.sub(r"[\s·ㆍ/,.\-'\"‘’“”]+", "", text or "")


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
    """키워드 개수 기반 점수. (pos - neg) / (pos + neg + 1).

    공백·구두점을 제거한 정규화 문자열에서 키워드를 찾으므로
    '사상 최고' 같은 띄어쓰기 변형도 매칭된다.
    """
    clean = _normalize(text)
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


def score_sentiment_llm(text: str, max_retries: int = 5) -> SentimentResult | None:
    """Claude API 로 의미 기반 점수. 키 없으면 None.

    429 rate-limit 에러는 지수 백오프로 최대 max_retries 회 재시도.
    """
    import time as _t
    import random as _r

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
    for attempt in range(max_retries):
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
            # 429 / overloaded 는 백오프 후 재시도
            msg = str(exc)
            is_rate = "429" in msg or "rate_limit" in msg or "overloaded" in msg.lower()
            if is_rate and attempt < max_retries - 1:
                # 지수 백오프 + 지터: 2, 4, 8, 16, 32s (+ ±20%)
                delay = (2 ** (attempt + 1)) * (0.8 + 0.4 * _r.random())
                _t.sleep(delay)
                continue
            logger.warning("LLM sentiment failed (attempt {}): {}", attempt + 1, msg[:150])
            return None
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
