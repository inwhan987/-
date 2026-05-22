"""뉴스 제목·요약 감성 분석.

1) 기본: 한국어 호재/악재 키워드 매칭 → -1 ~ +1 점수
2) 선택: ANTHROPIC_API_KEY 가 있으면 Claude 로 헤드라인을 의미적으로 분석
3) 심볼별 LLM 분류 사전(`data/llm_phrases_{symbol}.json`) 이 있으면
   critical 구 매칭 시 점수를 LLM bullishness 로 override 하고 weight 를 3으로.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from loguru import logger

LLM_PHRASES_DIR = Path("data")

POSITIVE_TERMS = [
    "호재", "급등", "상승", "흑자", "매수", "추천", "상향", "성장",
    "호실적", "신고가", "돌파", "사상최대", "최대실적", "흥행", "수혜", "호조",
    "낙관", "반등", "급등세", "강세", "목표가상향", "실적개선", "수주", "계약체결",
    "승인", "통과", "출시", "개선", "배당", "자사주매입",
    # 추가
    "역대최고", "사상최고", "최고가경신", "경신", "폭주", "질주", "랠리",
    "훈풍", "회복", "턴어라운드", "순항", "약진", "부양", "호황", "호평",
    "어닝서프라이즈", "깜짝실적", "최대매출", "흑자전환", "신기록",
    # 수급 호재 (명확한 복합어만)
    "무상증자", "자사주소각", "외국인순매수", "기관순매수", "연기금매수",
    "매출증가", "이익증가", "영업이익증가", "수익증가",
    # 실적 기대 상회
    "컨센서스상회", "예상상회", "기대상회", "가이던스상향",
]

NEGATIVE_TERMS = [
    "악재", "급락", "하락", "적자", "매도", "하향", "부진", "감익", "손실",
    "위기", "리스크", "의혹", "수사", "횡령", "배임", "감자", "유상증자",
    "파산", "법정관리", "거래정지", "상장폐지", "관리종목", "소송", "경고",
    "약세", "매도세", "목표가하향", "실적악화", "쇼크", "어닝쇼크", "부도",
    "급락세", "폭락", "디스카운트", "지연", "취소", "철회",
    # "연기"는 "연기금(pension fund)"에 오매칭 → 복합어로 교체
    "발표연기", "출시연기", "일정연기", "사업연기",
    # 시황·주가 추가
    "타격", "압박", "충격", "위축", "둔화", "후퇴", "적자전환", "경고등",
    "먹구름", "빨간불", "추락", "붕괴", "폐업", "제재", "과징금",
    "리콜", "불확실성", "우려확산", "매출감소", "수익성악화", "마진축소",
    "실망", "실적쇼크", "가이던스하향", "역성장",
    # 실적 기대 하회
    "컨센서스하회", "예상하회", "기대하회", "추정치하회", "전망하회",
    # 수급 악재
    "외국인순매도", "기관순매도",
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
    "컨센서스하회", "가이던스하향",
}


def _normalize(text: str) -> str:
    """공백·구두점 제거해 '사상 최고' → '사상최고' 같은 변형을 매칭 가능하게."""
    return re.sub(r"[\s·ㆍ/,.\-'\"‘’“”]+", "", text or "")


@dataclass
class SentimentResult:
    score: float  # -1 ~ +1
    positives: list[str]
    negatives: list[str]
    method: str  # "keyword" | "llm" | "llm_phrase"
    weight: float = 1.0   # 가중 평균용. critical 이벤트는 3.0
    is_critical: bool = False
    critical_phrases: list[str] | None = None


# ---------- LLM 분류 사전 로더 ----------
_LLM_PHRASE_CACHE: dict[str, list[dict]] = {}


def _load_llm_phrases(symbol: str) -> list[dict]:
    if symbol in _LLM_PHRASE_CACHE:
        return _LLM_PHRASE_CACHE[symbol]
    path = LLM_PHRASES_DIR / f"llm_phrases_{symbol}.json"
    if not path.exists():
        _LLM_PHRASE_CACHE[symbol] = []
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        # 긴 구 우선 (longest-match)
        entries.sort(key=lambda e: -len(e.get("phrase", "")))
        _LLM_PHRASE_CACHE[symbol] = entries
        logger.info("llm_phrases[{}] 로드: {} 엔트리", symbol, len(entries))
        return entries
    except Exception as exc:
        logger.warning("llm_phrases load 실패 {}: {}", path, exc)
        _LLM_PHRASE_CACHE[symbol] = []
        return []


def score_llm_phrases(text: str, symbol: str) -> tuple[float, list[str], bool] | None:
    """심볼별 LLM 분류 사전으로 점수 계산.

    Returns (score, matched_critical_phrases, is_critical) or None 사전 없음.
    longest-match 로 매칭되는 구의 bullishness 합산 → [-1, +1] 클립.
    """
    entries = _load_llm_phrases(symbol)
    if not entries or not text:
        return None
    clean = _normalize(text)
    masked = list(clean)
    matched: list[tuple[str, float, bool]] = []
    for e in entries:
        phrase = _normalize(e.get("phrase", ""))
        if not phrase:
            continue
        bull = float(e.get("bullishness", 0.0))
        crit = bool(e.get("is_critical", False))
        idx = 0
        while True:
            pos = "".join(masked).find(phrase, idx)
            if pos < 0:
                break
            if any(masked[i] == "\0" for i in range(pos, pos + len(phrase))):
                idx = pos + 1
                continue
            for i in range(pos, pos + len(phrase)):
                masked[i] = "\0"
            matched.append((e.get("phrase", ""), bull, crit))
            idx = pos + len(phrase)
    if not matched:
        return None
    total = sum(b for _, b, _ in matched)
    # 평균이 아닌 가중합 기반: 여러 개 매칭되면 서로 강화/상쇄
    score = max(-1.0, min(1.0, total))
    crit_phrases = [p for p, _, c in matched if c]
    return score, crit_phrases, bool(crit_phrases)


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


def score_sentiment_llm(text: str, max_retries: int = 5, symbol: str | None = None) -> SentimentResult | None:
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

    # 종목명 조회 (있으면 관련성 판단에 사용)
    company_name = ""
    if symbol:
        try:
            from stock_bot.names import get_name
            company_name = get_name(symbol) or ""
        except Exception:
            pass

    if company_name:
        prompt = (
            f"너는 한국 주식 투자 전문가다.\n"
            f"종목:{company_name} 헤드라인:{text}\n"
            f"점수-1~+1: 강호재+0.7↑(실적급증·수주·신약승인·어닝서프라이즈·자사주소각·무상증자) 호재+0.3↑ 중립±0.3 악재-0.3↓ 강악재-0.7↓(파업·파산·횡령·사고·어닝쇼크)\n"
            f"음수강제: 파업·노조·쟁의·소송·기소·구속·횡령·배임·과징금·적자·손실·리콜·사고·화재·유출·해킹·감자·유상증자·상장폐지·파산\n"
            f"실적맥락: 실적/매출+감소/하락/부진/하회/전년대비감소/컨센서스하회→음수, 단순발표→0, 증가/상회/서프라이즈→양수\n"
            f"수급맥락: 외국인/기관/연기금+순매수→양수, 순매도→음수\n"
            f"관련도: A={company_name}직접(실적·수주·노사·지배구조) B=섹터/시황/거시(반도체업황·글로벌증시·미증시·환율·금리·외국인수급) C=무관(삼성과 무관한 타업종·개별해외기업)\n"
            f"출력: 점수 관련도 예)-0.8 A 설명금지"
        )
    else:
        prompt = (
            f"너는 한국 주식 투자 전문가다.\n"
            f"헤드라인:{text}\n"
            f"점수-1~+1: 강호재+0.7↑(실적급증·수주·어닝서프라이즈·자사주소각·무상증자) 호재+0.3↑ 중립±0.3 악재-0.3↓ 강악재-0.7↓(파업·파산·사고·어닝쇼크)\n"
            f"음수강제: 파업·노조·소송·기소·구속·횡령·배임·과징금·적자·손실·리콜·사고·화재·유출·해킹·감자·유상증자·파산\n"
            f"실적맥락: 실적/매출+감소/하락/부진/하회/컨센서스하회→음수, 단순발표→0, 증가/상회→양수\n"
            f"수급맥락: 외국인/기관/연기금+순매수→양수, 순매도→음수\n"
            f"출력: 점수만(소수점1자리) 설명금지"
        )
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=20,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            match = re.search(r"-?\d+(\.\d+)?", raw)
            if not match:
                return None
            score = max(-1.0, min(1.0, float(match.group(0))))
            # 관련도 파싱: A=직접(1.0), B=간접(0.5), C=무관(0)
            relevance = "A"
            rel_match = re.search(r"\b([ABC])\b", raw)
            if rel_match:
                relevance = rel_match.group(1)
            if relevance == "C":
                score = 0.0
            elif relevance == "B":
                score = score * 0.7  # 섹터/시황도 개별 종목에 부분 관련 → 0.5에서 0.7로 조정
            try:
                from stock_bot.costs import record_cost
                record_cost("news_sentiment", resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
            except Exception:
                pass
            return SentimentResult(score=score, positives=[], negatives=[], method="llm")
        except Exception as exc:
            # 429 rate-limit / 529 overloaded 백오프 재시도
            msg = str(exc)
            is_overloaded = "529" in msg or "overloaded" in msg.lower()
            is_rate      = "429" in msg or "rate_limit" in msg
            if (is_overloaded or is_rate) and attempt < max_retries - 1:
                if is_overloaded:
                    # 529: API 과부하 → 최소 30s 기다린 후 지수 증가
                    delay = (30 + 20 * attempt) * (0.8 + 0.4 * _r.random())
                else:
                    # 429: 지수 백오프 2→4→8→16→32s (+±20%)
                    delay = (2 ** (attempt + 1)) * (0.8 + 0.4 * _r.random())
                logger.debug("LLM sentiment retry {}/{} in {:.0f}s ({})",
                             attempt + 1, max_retries, delay,
                             "overloaded" if is_overloaded else "rate_limit")
                _t.sleep(delay)
                continue
            logger.warning("LLM sentiment failed (attempt {}): {}", attempt + 1, msg[:150])
            return None
    return None


def score_sentiment_llm_batch(
    texts: list[str], symbol: str | None = None, max_retries: int = 5
) -> list[SentimentResult | None]:
    """여러 헤드라인을 LLM 1회 호출로 일괄 분석.

    Returns: texts 와 같은 길이의 결과 리스트 (실패 시 None).
    """
    import time as _t
    import random as _r

    if not texts:
        return []

    # 빈 문자열 필터링 — 빈 항목이 있으면 LLM이 "뉴스 없음" 대화 응답을 반환함
    valid_indices: list[int] = []
    valid_texts: list[str] = []
    for i, t in enumerate(texts):
        if t and t.strip():
            valid_indices.append(i)
            valid_texts.append(t.strip())
    if not valid_texts:
        return [None] * len(texts)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return [None] * len(texts)
    try:
        from anthropic import Anthropic
    except ImportError:
        return [None] * len(texts)

    client = Anthropic(api_key=api_key)

    company_name = ""
    if symbol:
        try:
            from stock_bot.names import get_name
            company_name = get_name(symbol) or ""
        except Exception:
            pass

    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(valid_texts))
    system_prompt = "JSON 배열만 출력. 설명·마크다운·줄바꿈 금지."
    if company_name:
        prompt = (
            f"너는 한국 주식 투자 전문가다.\n"
            f"종목:{company_name}\n"
            f"점수-1~+1: 강호재+0.7↑(실적급증·수주·신약승인·어닝서프라이즈·자사주소각·무상증자) 호재+0.3↑ 중립±0.3 악재-0.3↓ 강악재-0.7↓(파업·파산·횡령·사고·어닝쇼크)\n"
            f"음수강제: 파업·노조·쟁의·소송·기소·구속·횡령·배임·과징금·적자·손실·리콜·사고·화재·유출·해킹·감자·유상증자·상장폐지·파산\n"
            f"실적맥락: 실적/매출+감소/하락/부진/하회/전년대비감소/컨센서스하회→음수, 단순발표→0, 증가/상회/서프라이즈→양수\n"
            f"수급맥락: 외국인/기관/연기금+순매수→양수, 순매도→음수\n"
            f"관련도: A={company_name}직접(실적·수주·노사·지배구조) B=섹터/시황/거시(반도체업황·글로벌증시·미증시·환율·금리·외국인수급) C=무관(삼성과 무관한 타업종·개별해외기업)\n"
            f"출력:[[점수,\"관련도\"],...] 예:[[0.8,\"A\"],[-0.7,\"A\"],[0.0,\"C\"]]\n\n"
            f"{numbered}"
        )
    else:
        prompt = (
            f"너는 한국 주식 투자 전문가다.\n"
            f"점수-1~+1: 강호재+0.7↑(실적급증·수주·어닝서프라이즈·자사주소각·무상증자) 호재+0.3↑ 중립±0.3 악재-0.3↓ 강악재-0.7↓(파업·파산·사고·어닝쇼크)\n"
            f"음수강제: 파업·노조·소송·기소·구속·횡령·배임·과징금·적자·손실·리콜·사고·화재·유출·해킹·감자·유상증자·파산\n"
            f"실적맥락: 실적/매출+감소/하락/부진/하회/컨센서스하회→음수, 단순발표→0, 증가/상회→양수\n"
            f"수급맥락: 외국인/기관/연기금+순매수→양수, 순매도→음수\n"
            f"출력:[점수,...] 예:[0.8,-0.7,0.0]\n\n"
            f"{numbered}"
        )

    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max(60, len(valid_texts) * 12),
                system=system_prompt,
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "["},
                ],
            )
            raw = "[" + resp.content[0].text.strip()  # 프리필 "[" 복원
            try:
                from stock_bot.costs import record_cost
                record_cost("news_sentiment", resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
            except Exception:
                pass

            # JSON 파싱: [[점수, 관련도], ...] 또는 [점수, ...]
            import json as _json
            json_m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not json_m:
                logger.warning("batch JSON 없음: {!r}", raw[:120])
                return [None] * len(texts)
            # LLM 출력 정규화: 유니코드 마이너스(−) → 하이픈(-), 양수 부호(+) 제거
            json_str = json_m.group(0)
            json_str = json_str.replace("−", "-")   # − (U+2212) → -
            json_str = json_str.replace("–", "-")   # – (U+2013 en-dash) → -
            json_str = re.sub(r"(?<![eE])\+(?=\d)", "", json_str)
            json_str = re.sub(r"(-|\s|,|\[)\.(\d)", r"\g<1>0.\2", json_str)  # -.5 → -0.5, .5 → 0.5

            def _parse_item(item) -> tuple[float, str] | None:
                try:
                    # [[score, rel]] → [score, rel] 이중 래핑 풀기
                    if isinstance(item, list) and len(item) == 1 and isinstance(item[0], list):
                        item = item[0]
                    if isinstance(item, list):
                        s = float(item[0])
                        r = str(item[1]).upper() if len(item) > 1 else "A"
                        return s, r
                    else:
                        return float(item), "A"
                except (TypeError, ValueError, IndexError):
                    return None

            # valid_texts 결과를 원래 texts 인덱스에 매핑
            results: list[SentimentResult | None] = [None] * len(texts)
            try:
                data = _json.loads(json_str)
            except _json.JSONDecodeError as je:
                # 일부 항목이 잘못된 경우 숫자만 추출해 fallback
                logger.debug("batch JSON 파싱 실패({}) raw={!r}", je, raw[:150])
                nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
                for vi, n in enumerate(nums[:len(valid_texts)]):
                    try:
                        score = max(-1.0, min(1.0, float(n)))
                        results[valid_indices[vi]] = SentimentResult(score=score, positives=[], negatives=[], method="llm")
                    except ValueError:
                        pass
                return results

            for vi, item in enumerate(data):
                if vi >= len(valid_texts):
                    break
                parsed = _parse_item(item)
                if parsed is None:
                    logger.debug("batch item[{}] 파싱 불가: {!r}", vi, item)
                    continue
                raw_score, relevance = parsed
                score = max(-1.0, min(1.0, raw_score))
                if relevance == "C":
                    score = 0.0
                elif relevance == "B":
                    score *= 0.7  # 섹터/시황도 개별 종목에 부분 관련 → 0.5에서 0.7로 조정
                results[valid_indices[vi]] = SentimentResult(score=score, positives=[], negatives=[], method="llm")
            return results
        except Exception as exc:
            msg = str(exc)
            is_overloaded = "529" in msg or "overloaded" in msg.lower()
            is_rate      = "429" in msg or "rate_limit" in msg
            if (is_overloaded or is_rate) and attempt < max_retries - 1:
                if is_overloaded:
                    # 529: API 과부하 → 최소 30s 기다린 후 지수 증가
                    delay = (30 + 20 * attempt) * (0.8 + 0.4 * _r.random())
                else:
                    # 429: 지수 백오프 2→4→8→16→32s (+±20%)
                    delay = (2 ** (attempt + 1)) * (0.8 + 0.4 * _r.random())
                logger.debug("LLM batch retry {}/{} in {:.0f}s ({})",
                             attempt + 1, max_retries, delay,
                             "overloaded" if is_overloaded else "rate_limit")
                _t.sleep(delay)
                continue
            logger.warning("LLM batch sentiment failed (attempt {}): {}", attempt + 1, msg[:150])
            return [None] * len(texts)
    return [None] * len(texts)


def score_sentiment(
    text: str, prefer_llm: bool = False, symbol: str | None = None
) -> SentimentResult:
    """종합 감성 점수.

    우선순위:
      1) symbol 의 LLM 분류 사전에 critical 매칭 → bullishness 로 override,
         weight=3.0, method=llm_phrase, is_critical=True.
      2) prefer_llm 이면 Claude LLM 호출.
      3) 기본 키워드 매칭.
    LLM 분류 사전에 비critical 매칭만 있어도 2/3 결과와 평균해 보정할 수
    있지만, 여기서는 단순히 1) 만 override 로 동작하게 한다.
    """
    if symbol:
        phrased = score_llm_phrases(text, symbol)
        if phrased is not None:
            score, crit_phrases, is_critical = phrased
            if is_critical:
                return SentimentResult(
                    score=score,
                    positives=[],
                    negatives=[],
                    method="llm_phrase",
                    weight=3.0,
                    is_critical=True,
                    critical_phrases=crit_phrases,
                )
    if prefer_llm:
        res = score_sentiment_llm(text, symbol=symbol)
        if res is not None:
            return res
    return score_sentiment_keyword(text)


def summarize_symbol_sentiment(results: list[SentimentResult]) -> float:
    """여러 기사의 점수를 평균. 최근일수록 가중은 호출 측 책임."""
    if not results:
        return 0.0
    return sum(r.score for r in results) / len(results)
