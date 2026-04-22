"""뉴스 키워드 → 주가 영향 분석.

입력: news_backfill.db (백필된 기사 + published_at)
가격: yfinance 시간봉 (005930.KS)
지표: 기사 발행 시각 이후 +1h/+4h/+1d 수익률(로그수익률)
집계: 제목 키워드별 (count, mean_ret, median_ret, win_rate)

사용:
    python -m stock_bot.news.impact --db news_backfill.db --symbols 005930 \
        --horizon 1h --top 40 --min-count 5 --out keyword_impact.csv
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from math import sqrt
from statistics import mean, median, pstdev

import pandas as pd
import yfinance as yf
from loguru import logger

from stock_bot.news.sentiment import NEGATIVE_TERMS, POSITIVE_TERMS

try:
    from kiwipiepy import Kiwi

    _KIWI: "Kiwi | None" = None

    def _get_kiwi() -> "Kiwi":
        global _KIWI
        if _KIWI is None:
            _KIWI = Kiwi()
        return _KIWI
except ImportError:  # kiwipiepy 미설치 시 구 추출 비활성
    Kiwi = None  # type: ignore

    def _get_kiwi():
        return None

HORIZONS = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}

# KOSPI 지수 티커 (yfinance)
KOSPI_TICKER = "^KS11"

# 불용어: 통계적으로 의미 없는 일반어·접속사·조사 포함된 토큰.
STOPWORDS: set[str] = {
    # 접속/지시
    "각각", "한편", "또한", "그리고", "이는", "이에", "이날", "당시", "이후", "이전",
    "오늘", "어제", "내일", "지난", "이번", "다음", "전일", "금일", "최근", "현재",
    # 조사/어미형
    "등이", "등을", "등은", "에도", "에서", "으로", "에게", "있는", "있다", "있으며",
    "됐다", "됐고", "됐으며", "되는", "됐던", "하는", "하며", "하고", "했다", "한다",
    "했던", "했고", "했으며", "위해", "따라", "통해", "대한", "대해", "관련",
    # 일반
    "기자", "뉴스", "기사", "보도", "발표", "내용", "가운데", "사이", "만큼", "정도",
    "이런", "저런", "그런", "이같은", "그러나", "하지만", "다만", "특히",
    # 일·시간 숫자 흔한 패턴
    "오전", "오후", "올해", "올", "내년", "작년", "전년",
    # 고유명사 중 잡음
    "서울", "한국", "미국", "중국", "일본", "삼성", "전자", "삼성전자",
}

# 숫자 + 단위 형태 (예: "26일", "50조", "1.5만", "3%")
_NUM_UNIT_RE = re.compile(r"^-?\d+(\.\d+)?[일월년시분초%조억만천원달러배주]*$")
_PURE_ALNUM_RE = re.compile(r"^[0-9A-Za-z]+$")
_HANGUL_RE = re.compile(r"[가-힣]")
# 한국어 동사/형용사 활용 어미로 끝나는 토큰(의미 없는 어형) 제거
_EOMI_SUFFIX = (
    "하는", "되는", "있는", "없는", "같은", "라는",
    "했다", "한다", "된다", "있다", "없다", "한편",
    "하고", "하며", "해서", "돼서", "되며",
    "했던", "됐던", "했고", "됐고",
    "이고", "이며", "이나", "이라",
    "으로", "에서", "에게", "에도", "까지", "부터",
    "만큼", "처럼", "보다",
    "비롯해", "통해", "위해", "따라", "대해", "관해",
    "일제히", "빠르게", "가까이", "멀리",
    "펼치는", "펼친", "맞춰", "맞춘", "기대감에", "기대에", "우려에",
    "들어", "들어간", "들어온", "지난달", "이달", "이번달",
    "했으며", "됐으며", "정책을", "정책의", "중인",
)


@dataclass
class Article:
    symbol: str
    title: str
    summary: str
    published_at: datetime
    existing_score: float


def load_articles(db_path: str, symbols: list[str]) -> list[Article]:
    conn = sqlite3.connect(db_path)
    placeholders = ",".join("?" * len(symbols))
    rows = conn.execute(
        f"""SELECT symbol, title, summary, published_at, sentiment_score
            FROM news WHERE symbol IN ({placeholders})""",
        symbols,
    ).fetchall()
    conn.close()
    out: list[Article] = []
    for sym, title, summary, pub, score in rows:
        try:
            ts = datetime.fromisoformat(pub)
        except ValueError:
            continue
        out.append(Article(sym, title or "", summary or "", ts, float(score)))
    return out


def fetch_hourly(symbol_kr: str, days: int) -> pd.DataFrame:
    """yfinance 로 한국주식 시간봉. 005930 → 005930.KS."""
    # ^로 시작(지수), . 포함(이미 suffix), 숫자 아님(이미 티커) → 그대로.
    # 순수 6자리 숫자만 .KS 접미사.
    if symbol_kr.startswith("^") or "." in symbol_kr or not symbol_kr.isdigit():
        ticker = symbol_kr
    else:
        ticker = f"{symbol_kr}.KS"
    period_days = max(days + 2, 7)
    df = yf.download(
        ticker,
        period=f"{period_days}d",
        interval="1h",
        auto_adjust=True,
        progress=False,
    )
    if df.empty:
        logger.warning("가격 데이터 없음: {}", ticker)
        return df
    # tz-aware → naive(KST 아닌 원본 tz 제거)
    df.index = df.index.tz_convert(None) if df.index.tz is not None else df.index
    # columns MultiIndex 평탄화
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Close"]].rename(columns={"Close": "close"})


def _match_bars(
    t0: pd.Timestamp, horizon: timedelta, bars: pd.DataFrame
) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """뉴스 시각 t0 기준으로 (pre_bar, post_bar) 를 반환.

    - pre_bar:  t0 **직전** 에 이미 닫힌 봉 (= index+1h ≤ t0 인 마지막 봉).
                이 봉의 종가가 "뉴스 나오기 직전 가격".
    - post_bar: t0 + horizon **까지** 닫힌 마지막 봉 (= index+1h ≤ t0+horizon).
                이 봉의 종가가 "뉴스 후 horizon 시점 가격".
    이렇게 잡아야 뉴스 직후 즉시 반응이 수익률에 포함된다.
    """
    idx = bars.index
    bar_len = pd.Timedelta("1h")
    pre_candidates = idx[idx + bar_len <= t0]
    if len(pre_candidates) == 0:
        return None
    pre = pre_candidates[-1]
    post_target = t0 + horizon
    post_candidates = idx[idx + bar_len <= post_target]
    if len(post_candidates) == 0:
        return None
    post = post_candidates[-1]
    if post <= pre:
        return None  # horizon 내 새 봉이 닫히지 않음
    return pre, post


def article_return_with_bar(
    article: Article,
    bars: pd.DataFrame,
    horizon: timedelta,
    bench: pd.DataFrame | None = None,
) -> tuple[float, pd.Timestamp] | None:
    """수익률과 매칭된 시작봉 타임스탬프를 함께 반환."""
    r = article_return(article, bars, horizon, bench)
    if r is None or bars.empty:
        return None
    t0 = pd.Timestamp(article.published_at)
    m = _match_bars(t0, horizon, bars)
    if m is None:
        return None
    return r, m[0]


def article_return(
    article: Article,
    bars: pd.DataFrame,
    horizon: timedelta,
    bench: pd.DataFrame | None = None,
) -> float | None:
    """기사 발행 직후 첫 봉 종가 → horizon 후 봉 종가 수익률.

    bench 가 주어지면 벤치마크 대비 **초과수익률**(stock_ret - bench_ret) 을 반환.
    """
    if bars.empty:
        return None
    t0 = pd.Timestamp(article.published_at)
    stk = _match_bars(t0, horizon, bars)
    if stk is None:
        return None
    ts, te = stk
    p0, p1 = float(bars.loc[ts, "close"]), float(bars.loc[te, "close"])
    if p0 <= 0:
        return None
    ret = (p1 - p0) / p0
    if bench is None or bench.empty:
        return ret
    # 벤치마크는 기사 시각 기준으로 같은 구간을 잡음
    bm = _match_bars(t0, horizon, bench)
    if bm is None:
        return ret  # 벤치 구멍이면 그냥 raw
    bs, be = bm
    b0, b1 = float(bench.loc[bs, "close"]), float(bench.loc[be, "close"])
    if b0 <= 0:
        return ret
    bench_ret = (b1 - b0) / b0
    return ret - bench_ret


_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z가-힣]+")
_WHITELIST_ALNUM = {"AI", "HBM", "ESG", "IPO", "EV", "PC", "IT", "5G", "6G", "D램"}


def _is_noise(tok: str) -> bool:
    if tok in STOPWORDS:
        return True
    if len(tok) < 2:
        return True
    if _NUM_UNIT_RE.match(tok):
        return True  # "26일", "50조", "3%" 등
    # 영어·숫자만: 화이트리스트 외엔 버림
    if _PURE_ALNUM_RE.match(tok) and tok not in _WHITELIST_ALNUM:
        return True
    # 한글이 없는 토큰 중 길이 2 짜리는 보통 잡음
    if not _HANGUL_RE.search(tok) and len(tok) < 3 and tok not in _WHITELIST_ALNUM:
        return True
    # 활용형 어미 (조사·연결어미)
    if tok in _EOMI_SUFFIX:
        return True
    # 길이 3+ 이면서 흔한 조사·어미로 끝나면 버림
    if len(tok) >= 3 and tok.endswith(
        ("으로", "에서", "에게", "에도", "까지", "부터", "이며", "이고", "했다", "한다")
    ):
        return True
    return False


def tokenize(text: str) -> list[str]:
    """공백·구두점·특수문자로 분리. 불용어·숫자단위·영문잡음 제거."""
    return [t for t in _TOKEN_SPLIT.split(text or "") if not _is_noise(t)]


def extract_noun_phrases(text: str, max_len: int = 6, min_len: int = 1) -> list[str]:
    """kiwi 로 명사(NN*) + 명사접미사(XSN) 를 묶은 뒤, 연속 명사 구 n-gram 생성.

    예: '코스피 6,400선 근접 사상 최고치 달성' →
        [코스피, 선, 근접, 사상, 최고치, 달성,
         코스피 선, 선 근접, ..., 최고치 달성,
         근접 사상 최고치, 사상 최고치 달성, ...]
    """
    kiwi = _get_kiwi()
    if kiwi is None or not text:
        return []
    tokens = kiwi.tokenize(text)
    # 1) 명사 계열만 남기되, XSN(명사접미사)은 직전 명사에 붙임
    nouns: list[str] = []
    for tok in tokens:
        tag = tok.tag
        form = tok.form
        if tag.startswith("NN"):  # NNG, NNP, NNB
            nouns.append(form)
        elif tag == "XSN" and nouns:
            nouns[-1] = nouns[-1] + form
        else:
            # 비명사 → 구 경계
            nouns.append("\0")
    # 2) 경계로 분리된 연속 명사 그룹 생성
    groups: list[list[str]] = []
    cur: list[str] = []
    for n in nouns:
        if n == "\0":
            if cur:
                groups.append(cur)
                cur = []
        else:
            if len(n) >= 2:  # 1글자 명사 배제
                cur.append(n)
    if cur:
        groups.append(cur)
    # 3) 그룹 내 n-gram (1~max_len) 추출
    out: list[str] = []
    for g in groups:
        for n in range(min_len, max_len + 1):
            for i in range(len(g) - n + 1):
                phrase = " ".join(g[i : i + n])
                out.append(phrase)
    return out


def extract_keywords(
    text: str,
    use_phrases: bool = True,
    phrase_min_len: int = 1,
    phrase_max_len: int = 6,
    include_words: bool = True,
) -> set[str]:
    """기본 키워드(호재/악재 사전) + (옵션) 단어 토큰화 + (옵션) 명사구.

    phrase_min_len=2 로 주면 단일 명사는 배제되고 2단어 이상 구만 수집.
    include_words=False 면 `tokenize()` 결과도 배제 (구만 남김).
    """
    keys: set[str] = set()
    normalized = re.sub(r"[\s·ㆍ/,.\-'\"‘’“”]+", "", text or "")
    for term in POSITIVE_TERMS + NEGATIVE_TERMS:
        if term in normalized:
            keys.add(term)
    if include_words:
        keys.update(tokenize(text))
    if use_phrases:
        for p in extract_noun_phrases(
            text, max_len=phrase_max_len, min_len=phrase_min_len
        ):
            # 구 구성요소가 전부 불용어면 제외
            parts = p.split()
            if all(part in STOPWORDS for part in parts):
                continue
            keys.add(p)
    return keys


def main() -> None:
    ap = argparse.ArgumentParser(description="키워드-주가 영향 분석")
    ap.add_argument("--db", type=str, default="news_backfill.db")
    ap.add_argument("--symbols", type=str, default="005930")
    ap.add_argument("--horizon", choices=list(HORIZONS), default="1h")
    ap.add_argument("--days", type=int, default=30, help="가격 데이터 조회 기간")
    ap.add_argument("--top", type=int, default=40, help="상/하위 N 개 출력")
    ap.add_argument("--min-count", type=int, default=10, help="키워드 최소 등장수")
    ap.add_argument(
        "--min-tstat",
        type=float,
        default=2.0,
        help="|t-statistic| 최소치(평균/표준오차). 0 이면 필터 없음",
    )
    ap.add_argument(
        "--title-only",
        action="store_true",
        default=True,
        help="요약 제외, 제목만 토큰화 (기본 True)",
    )
    ap.add_argument(
        "--include-summary",
        dest="title_only",
        action="store_false",
        help="요약도 포함",
    )
    ap.add_argument(
        "--no-hour-dedup",
        dest="hour_dedup",
        action="store_false",
        default=True,
        help="시간 버킷당 키워드별 1회 중복제거 비활성",
    )
    ap.add_argument(
        "--no-phrases",
        dest="use_phrases",
        action="store_false",
        default=True,
        help="kiwi 기반 명사구 추출 비활성 (단어만 사용)",
    )
    ap.add_argument(
        "--phrase-min-len",
        type=int,
        default=1,
        help="명사구 최소 토큰 수. 2 이상이면 단일명사 배제 (예: '공급' X, '공급 계약' O)",
    )
    ap.add_argument(
        "--phrase-max-len",
        type=int,
        default=6,
        help="명사구 최대 토큰 수. '메모리 공급 과잉 우려' 같은 긴 구 잡으려면 5~6",
    )
    ap.add_argument(
        "--phrases-only",
        dest="include_words",
        action="store_false",
        default=True,
        help="단일 단어 토큰 제외하고 명사구만 집계",
    )
    ap.add_argument(
        "--out", type=str, default="keyword_impact.csv", help="전체 결과 CSV 경로"
    )
    ap.add_argument(
        "--benchmark",
        type=str,
        default=KOSPI_TICKER,
        help="벤치마크 티커(KOSPI=^KS11). 'none' 이면 raw 수익률",
    )
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    horizon = HORIZONS[args.horizon]

    articles = load_articles(args.db, symbols)
    logger.info("기사 로드: {}건", len(articles))
    if not articles:
        raise SystemExit("기사가 없습니다. 먼저 backfill 로 채우세요.")

    # 종목별 시간봉 캐시
    price_cache: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        price_cache[sym] = fetch_hourly(sym, args.days)
        logger.info("[{}] 시간봉 {} 행", sym, len(price_cache[sym]))

    # 벤치마크 (KOSPI) 시간봉
    bench_df: pd.DataFrame | None = None
    use_bench = args.benchmark.lower() != "none"
    if use_bench:
        bench_df = fetch_hourly(args.benchmark, args.days)
        logger.info("[{}] 벤치마크 시간봉 {} 행", args.benchmark, len(bench_df))
        if bench_df.empty:
            logger.warning("벤치마크 비어있음 → raw 수익률로 fallback")
            bench_df = None

    # 기사별 (수익률, 시작봉) 계산
    scored: list[tuple[Article, float, pd.Timestamp]] = []
    per_symbol_count: dict[str, int] = defaultdict(int)
    missing = 0
    for a in articles:
        bars = price_cache.get(a.symbol)
        if bars is None or bars.empty:
            missing += 1
            continue
        result = article_return_with_bar(a, bars, horizon, bench=bench_df)
        if result is None:
            missing += 1
            continue
        r, t_start = result
        scored.append((a, r, t_start))
        per_symbol_count[a.symbol] += 1
    logger.info(
        "수익률 매칭: {}건 성공 / {}건 누락 (장외시간·데이터밖). 종목별 {} bench={}",
        len(scored),
        missing,
        dict(per_symbol_count),
        "KOSPI-초과" if bench_df is not None else "raw",
    )
    if not scored:
        raise SystemExit("수익률 매칭이 전부 실패했습니다.")

    # 키워드 → 수익률 (옵션: 동일 (심볼, 시작봉) 에 대해 키워드당 1회만)
    kw_returns: dict[str, list[float]] = defaultdict(list)
    seen_bar: dict[str, set[tuple]] = defaultdict(set)
    for a, r, t_start in scored:
        text = a.title if args.title_only else f"{a.title} {a.summary}"
        bar_key = (a.symbol, t_start)
        for kw in extract_keywords(
            text,
            use_phrases=args.use_phrases,
            phrase_min_len=args.phrase_min_len,
            phrase_max_len=args.phrase_max_len,
            include_words=args.include_words,
        ):
            if args.hour_dedup and bar_key in seen_bar[kw]:
                continue
            seen_bar[kw].add(bar_key)
            kw_returns[kw].append(r)

    # 집계 + t-통계량 필터
    rows = []
    for kw, rets in kw_returns.items():
        n = len(rets)
        if n < args.min_count:
            continue
        mu = mean(rets)
        sd = pstdev(rets) if n >= 2 else 0.0
        se = sd / sqrt(n) if sd > 0 else 0.0
        t_stat = mu / se if se > 0 else 0.0
        if args.min_tstat > 0 and abs(t_stat) < args.min_tstat:
            continue
        wins = sum(1 for x in rets if x > 0)
        rows.append(
            {
                "keyword": kw,
                "count": n,
                "mean_ret": mu,
                "median_ret": median(rets),
                "win_rate": wins / n,
                "t_stat": t_stat,
            }
        )
    if not rows:
        raise SystemExit(
            f"min-count={args.min_count} 이상 키워드가 없습니다. 값을 낮춰보세요."
        )

    df = pd.DataFrame(rows).sort_values("mean_ret", ascending=False)
    out_path = Path(args.out).resolve()
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    logger.info("전체 결과 저장: {} ({} 키워드)", out_path, len(df))

    bench_label = f"KOSPI({args.benchmark}) 초과" if bench_df is not None else "raw"
    print(
        f"\n=== 기준: {args.horizon} {bench_label} 수익률, "
        f"min-count={args.min_count}, 종목={symbols} ==="
    )
    print(f"\n[TOP {args.top} 긍정 키워드]")
    print(df.head(args.top).to_string(index=False))
    print(f"\n[TOP {args.top} 부정 키워드]")
    print(df.tail(args.top).iloc[::-1].to_string(index=False))


if __name__ == "__main__":
    main()
