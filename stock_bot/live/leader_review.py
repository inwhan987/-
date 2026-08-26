"""대장주봇 전용 장마감 리뷰 (스톡봇/앙상블 리뷰와 분리).

앙상블 리뷰(review.py)는 '체결 로그'가 곧 데이터지만, 대장주봇은 하루 체결이
0~1건이라 체결만 보면 볼 게 없다. 정보의 대부분은 **'왜 안 샀나'** 에 있다.
그래서 이 리뷰는 체결 리뷰가 아니라 **깔때기(funnel) 리뷰**로 만든다.

  1. 선별  — 오늘 섹터 순위/점수, 재선별로 1등 섹터가 바뀌었는지
  2. 감시  — 감시 바스켓, 보류/신호스킵/미진입 건수와 사유 분포
  3. 체결  — 진입·청산 왕복, net%, **MFE/MAE**
  4. 반사실 — 감시했는데 안 산 종목의 당일 최대 상승폭(놓친 폭)
  5. 보유 중 섹터전환 반사실 — 보유하는 동안 신1등 섹터로 갈아탔다면?

## 5단계(보유 중 전환 반사실)를 넣는 이유
"섹터 순위가 밀리면 갈아타자"는 아이디어를 코드로 넣기 전에 값어치부터 재려고
만든 관측 장치다. 매매에는 전혀 개입하지 않고 숫자만 쌓는다. 재료는 이미 있다 —
재선별마다 leader_finder 가 남기는 {date}_reval_history.jsonl 에 시각·섹터점수·
후보 종목코드가 들어 있고, 리뷰는 장 마감 직후(15:40)에 돌므로 후보 종목의 당일
분봉을 그 자리에서 조회할 수 있다(KIS 는 당일치만 준다).

계산은 반드시 **같은 시점에서 갈라지는 두 선택**을 비교한다: 시점 T 에서 (a) 계속
보유 -> 실제 청산가, (b) 전환 -> 후보를 T 에 사서 같은 청산 시각까지 보유. 전환
왕복비용(매도수수료+매수수수료)은 (b) 에서만 뺀다. 중간 고가로 재면 안 된다 —
신1등은 정의상 '지금 제일 오른 섹터'라 사후편향이 크고, 오후에 되밀리는 게 흔하다.

## MFE/MAE 를 넣는 이유
08-24 리뷰가 "+0.18% 에 그쳐 엣지 소멸"이라고 단정했는데, 그 포지션이 장중에
+2% 까지 갔다 되밀린 건지 하루종일 정체였는지는 리포트로 알 수 없었다. 둘은
처방이 정반대다(전자=청산 문제, 후자=진입 문제). 진입가·이후 고가·저가만
있으면 갈리는 문제라 분봉에서 계산해 넣는다.

## LLM 호출 주기
대장주는 표본이 하루 0~2건이라 매일 LLM 해석을 돌리면 단발 표본으로 규칙을
만들자는 제안이 나온다(08-24 '진행부진 조기청산' = 이미 기각된 시간손절).
그래서 매일은 **기계적 팩트시트**만 만들고, LLM 해석은 금요일 또는 미리뷰
체결이 N건 누적됐을 때만 돈다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from stock_bot.config import settings as _settings
from stock_bot.market_calendar import KST as _KST
from stock_bot.notify import notify
from stock_bot.storage import ENGINE, ReviewLog, TradeLog, record_review

_ROOT = Path(__file__).resolve().parents[2]
_PICKS_DIR = _ROOT / "data" / "leader_picks"
_STATE_DIR = _ROOT / "data" / "leader_trade_state"
_LIVE_LOG = _ROOT / "logs" / "stock_leader.log"

_LEADER_STRATEGIES = ("leader_vwap_touch", "leader_pullback")
_BUY_COMM = 0.00015    # leader_trader.py:50-51 과 동일해야 수치가 맞다
_SELL_COMM = 0.00195

# 이미 검증 후 기각된 방향 — 프롬프트에 고정 주입해 재탕 제안을 막는다.
# 이게 없으면 리뷰가 매번 시간손절/조기청산/트레일링을 새 아이디어처럼 제안한다.
_REJECTED = """\
## 이미 백테스트로 검증 후 기각된 제안 (다시 제안하지 마라)
- 시간손절 / 진행부진 조기청산 / 보유시간 상한 — 스윕 전멸
- 트레일링 스톱, ATR 배수 손절, ATR 기반 사이징 — 기각
- 서킷브레이커, 오버나이트 청산, 시간대 컷 — 기각
- 대장주 청산규칙 전수 스윕(2026-08-18) — 현행 고정 규칙이 최적
- 체급별 지표 파라미터 분리(2026-07-15) — 기각
- 섹터 점수가 빠졌다고 보유 포지션 갈아타기 — 진입 조건 자체가 '눌림목'이라
  진입 직후가 점수 최저점이다. 갈아타기는 곧 최저점 매도 규칙이 된다.
  2026-08-24 부터 5단계에서 반사실만 측정 중이다. 채택 문턱은 보유일 15일 이상
  · 전환 우위 승률 60% 이상 · 평균 우위 +0.9%p 이상(왕복비용 0.3%의 3배)이고,
  미달이면 정식 기각한다. 누적 표본이 문턱에 못 미치는 동안은 제안하지 마라.
※ 진입·청산 레이어는 스윕이 끝났다. 남은 여지는 **선별·감시 레이어**뿐이다.
"""

_SYSTEM = """\
너는 한국 주식 단기 자동매매 시스템을 운용·개선해온 퀀트 트레이더다.
지금 평가 대상은 **대장주봇(leader)** 하나뿐이다. 앙상블(스톡봇)은 별도 리뷰가
있으니 언급하지 마라.

## 대장주 전략 요약
- 매일 09:30 선별: 자격필터 → 섹터 점수화 → 상위 섹터의 1~3등 종목을 감시
  바스켓에 편입 (장중 재선별로 섹터 순위를 갱신)
- 진입: 감시종목 분봉의 VWAP 눌림 / 스윙저점 눌림 + **회복확인** (눌린 것을 산다)
- 청산: 고정 익절 / 스윙저점 기준 손절 / 마감 청산
- 하루 체결 0~2건이 정상이다. 체결 0건은 그 자체로 실패가 아니다.
- **파라미터의 현재값은 팩트시트 '0. 현재 설정'에 그대로 실려 있다. 제안할 때는
  반드시 그 값을 현재값으로 인용하고, 거기 없는 손잡이는 지어내지 마라.**

## 리뷰 원칙
- 칭찬·일반론 금지. 단발 표본으로 규칙을 만들지 마라.
- **깔때기로 읽어라**: 선별 → 감시 → 신호 → 체결 중 어디서 몇 개가 걸러졌나.
  체결이 0이어도 '감시 N종목 중 눌림 M회, 회복확인 실패 K회'가 진짜 데이터다.
- MFE(최대 유리 이동)/MAE(최대 불리 이동)를 먼저 보라. 실현 수익이 작아도
  MFE 가 컸다면 청산 문제, MFE 도 작았다면 진입(종목 선정) 문제다.
- 반사실(감시했는데 안 산 종목의 당일 최대 상승폭)이 반복해서 크면 신호가
  과보수적이라는 신호다. 반대면 미진입이 옳았다는 근거다.

""" + _REJECTED + """
반드시 JSON 객체 하나만 출력한다. 설명, 주석, 마크다운 펜스 금지.
{
  "summary": "1~3문장 한국어 총평",
  "findings": ["구체적 패턴/문제. 깔때기 어느 단계인지 명시", "..."],
  "suggestions": ["선별·감시·신호 레이어 제안(파라미터명·현재값·방향)", "..."]
}
findings/suggestions 각 0~5개. 근거가 부족하면 빈 배열을 반환하고 summary 에
'표본 부족'이라고 쓰는 게 낫다. 억지 제안 금지."""


def _bare(code: str) -> str:
    return str(code).split(".")[0].strip()


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ────────────────────────────── 1. 선별 ──────────────────────────────
def _stage_settings() -> list[str]:
    """0단계: 지금 돌고 있는 파라미터 값을 그대로 싣는다.

    앙상블 리뷰(review.py)는 임계값·가중치를 settings 에서 실시간 주입하는데
    대장주 리뷰만 이 블록이 없었다. 그래서 프롬프트는 '파라미터명·현재값·방향을
    명시하라'고 요구하면서 정작 현재값을 주지 않았고, LLM 은 하드코딩된 산문
    요약('+4% 익절')만 보고 추측할 수밖에 없었다. '오늘 왜 안 샀나'는 감시 사유와
    그때의 컷 값을 나란히 놔야 읽히므로, 팩트시트 맨 앞에 둔다.

    기각·미사용으로 못박힌 손잡이(fib·volfilter·fib_dynamic)는 싣지 않는다 —
    보이면 제안 대상이 되고, 그건 이미 닫힌 문이다.
    """
    g = lambda k, d=0: getattr(_settings, k, d)
    out = ["## 0. 현재 설정 (제안 시 이 값을 현재값으로 인용하라)"]

    slot = float(g("leader_slot_budget_krw", 0))
    if slot <= 0:
        budget = float(g("leader_budget_krw", 0)) / max(1, int(g("leader_max_positions", 1)))
        slot_txt = f"{budget:,.0f}원(총예산/슬롯)"
    else:
        slot_txt = f"{slot:,.0f}원(직접지정)"
    out.append(
        f"- 매매 {'ON' if g('leader_trade_enabled', False) else 'OFF(관전)'}"
        f" · 슬롯 {int(g('leader_max_positions', 1))}개 · 슬롯예산 {slot_txt}"
        f" · 봉 {int(g('leader_interval_min', 3))}분"
    )

    mode = str(g("leader_entry_mode", "or_mode"))
    out.append(
        f"- 진입 모드 {mode} · 회복확인 {'ON' if g('leader_reclaim', True) else 'OFF'}"
        f" · 장대양봉컷 {float(g('leader_bar_range_pct', 0)):.1f}%"
        + (" (끔)" if float(g("leader_bar_range_pct", 0)) <= 0 else "")
    )
    if mode in ("or_mode", "pullback", "vwap_touch"):
        sl = float(g("leader_vwap_min_slope_pct", 0))
        out.append(
            f"  · VWAP 분기: 터치허용 {float(g('leader_vwap_tol', 0)):.2f}%"
            f" · 붕괴컷 전고점 대비 -{float(g('leader_vwap_max_pull_pct', 0)):.1f}%"
            f" · 기울기컷 {sl:.2f}%" + ("(끔)" if sl <= 0 else "")
        )
    if mode in ("or_mode", "pullback"):
        anc = str(g("leader_anchor", "off"))
        phw = int(g("leader_phwin_min", 0))
        out.append(
            f"  · 스윙저점 분기: 좌우확인 {int(g('leader_w', 2))}봉"
            f" · 최대눌림 {float(g('leader_max_pull_pct', 0)):.1f}%"
            f" · 앵커 {anc}"
            + (f"(EMA{int(g('leader_anchor_ema', 20))} · tol {float(g('leader_anchor_tol', 0)):.1f}%)"
               if anc != "off" else "")
            + f" · 전고점윈도 {'9시부터 누적' if phw <= 0 else str(phw) + '분 롤링'}"
        )

    ex = str(g("leader_exit_mode", "fixed"))
    ex_txt = {
        "fixed": f"익절 +{float(g('leader_tp_pct', 0)):.1f}%",
        "trail": (f"발동 +{float(g('leader_trail_activate_pct', 0)):.1f}%"
                  f" · 고점갭 {float(g('leader_trail_gap_pct', 0)):.1f}%"),
        "split": (f"1차 +{float(g('leader_split_tp1_pct', 0)):.1f}%"
                  f"({float(g('leader_split_tp1_ratio', 0)):.0f}%)"
                  f" · 2차 +{float(g('leader_split_tp2_pct', 0)):.1f}%"),
    }.get(ex, ex)
    out.append(
        f"- 청산 {ex}: {ex_txt}"
        f" · 손절 스윙저점 -{float(g('leader_stop_buf_pct', 0)):.1f}%"
        f" · 마감청산 {g('leader_close_time', '?')}"
    )

    out.append(
        f"- 선별: 거래대금 상위 {int(g('leader_sel_top', 0))}(시장별)"
        f" · 등락률 ≥{float(g('leader_sel_rise_min', 0)):.1f}%"
        f" · 과열컷 {float(g('leader_sel_max_change', 0)):.1f}%"
        f" · 핫섹터 {int(g('leader_sel_hot_min', 0))}종목↑"
        f" · 거래대금배수 ≥{float(g('leader_sel_vol_mult', 0)):.1f}배"
    )
    out.append(
        f"  · 거래대금 하한 앵커 {float(g('leader_sel_min_value_eok', 0)):.0f}억"
        f"@{g('leader_sel_min_value_anchor_hhmm', '?')}"
        f" [floor {float(g('leader_sel_min_value_floor_eok', 0)):.0f}억"
        f" · cap {float(g('leader_sel_max_value_eok', 0)):.0f}억]"
        f" · 시총 ≥{float(g('leader_sel_min_cap_eok', 0)):.0f}억"
    )
    out.append(
        f"  · 밴드비율 {float(g('leader_band_ratio', 0)):.2f}(1등 점수 대비 편입 문턱)"
        f" · 자금흐름배수 clamp [{float(g('leader_mf_clamp_low', 0)):.1f},"
        f" {float(g('leader_mf_clamp_high', 0)):.1f}]"
        f" · 일봉추세게이트 {'ON' if g('leader_daily_trend_gate', False) else 'OFF'}"
    )

    if g("leader_switch_enabled", True):
        out.append(
            f"- 재선별·전환 ON · 주기 {int(g('leader_switch_interval_min', 0))}분"
            f" · {g('leader_switch_until', '?')} 까지 · 감시섹터 "
            f"{int(g('leader_switch_watch_sectors', 0))} · 최대섹터 "
            f"{int(g('leader_max_sectors', 0))}"
        )
        out.append(
            f"  · 전환문턱 신섹터 > 현섹터×"
            f"{1 + float(g('leader_sector_switch_threshold', 0)):.2f}"
            f" · 히스테리시스 {int(g('leader_switch_hysteresis', 0))}종목"
            f" · 급등보류 {float(g('leader_switch_move_max_pct', 0)):.1f}%"
        )
    else:
        out.append("- 재선별·전환 OFF")
    return out


def _stage_selection(date: str) -> list[str]:
    out = ["## 1. 선별"]
    base = _load_json(_PICKS_DIR / f"{date}.json")
    if not base:
        out.append("- 정본 picks 없음 (선별 미실행 또는 실패)")
        return out
    leaders = base.get("leaders") or []
    out.append(
        f"- 정본 {base.get('selected_at', '?')} · 섹터후보 {len(leaders)}개"
        f" · flow {base.get('flow_tier', '?')}"
    )
    for L in leaders[:5]:
        out.append(
            f"  · {L.get('sector', '?')} 섹터점수 {float(L.get('sector_score_100') or 0):.1f}"
            f" · 1등 {L.get('name', '?')} {float(L.get('change_pct') or 0):+.2f}%"
        )
    # 재선별 결과 — 1등 섹터가 바뀌었는지가 선별 안정성의 핵심 지표다.
    rev = _load_json(_PICKS_DIR / f"{date}_reval.json")
    if rev:
        rl = rev.get("leaders") or []
        top_now = rl[0].get("sector", "?") if rl else "?"
        top_base = leaders[0].get("sector", "?") if leaders else "?"
        changed = "변동없음" if top_now == top_base else "교체"
        out.append(
            f"- 최종 재선별 {rev.get('selected_at', '?')} · 1등 섹터 "
            f"{top_base} → {top_now} ({changed})"
        )
    hist = _PICKS_DIR / f"{date}_reval_history.jsonl"
    if hist.exists():
        try:
            n = sum(1 for ln in hist.read_text(encoding="utf-8").splitlines() if ln.strip())
            out.append(f"- 재선별 실행 {n}회")
        except Exception:
            pass
    return out


# ────────────────────────────── 2. 감시 ──────────────────────────────
_RE_SKIP = re.compile(r"leader_trader: (.+?) (보류|신호 스킵|미진입) [-—] (.+?)\s*$")
_RE_NOADD = re.compile(r"leader_trader: 섹터 미추가 [-—] (.+?) . (.+?)\s*$")
_RE_NUM = re.compile(r"[0-9][0-9,.]*%?")


def _leader_log_lines(date: str) -> list[str]:
    """그날 대장주 로그 라인. 스냅샷(data/logs/leader/{date}.log)이 있으면 그걸,
    없으면(당일 리뷰) 라이브 로그에서 해당 날짜 라인만 추린다."""
    snap = _ROOT / "data" / "logs" / "leader" / f"{date}.log"
    for p in (snap, _LIVE_LOG):
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = [ln for ln in txt.splitlines() if ln.startswith(date)]
        if lines:
            return lines
    return []


def _stage_watch(date: str, log_lines: list[str]) -> tuple[list[str], list[dict]]:
    """감시 단계 요약 + 감시 종목 목록(반사실 계산용)."""
    out = ["## 2. 감시·신호"]
    st = _load_json(_STATE_DIR / f"{date}.json")
    baskets = st.get("sector_baskets") or {}
    watched: list[dict] = []
    for sec, members in baskets.items():
        for m in members or []:
            watched.append({
                "code": _bare(m.get("code", "")),
                "name": m.get("name", ""),
                "sector": sec,
            })
    out.append(
        f"- 감시 {len(baskets)}섹터 / {len(watched)}종목"
        f" · 활성섹터 {st.get('active_sector_name', '?')}"
    )

    # 신호 판정 카운트 — '왜 안 샀나' 가 이 리뷰의 핵심 데이터다.
    buckets: dict[str, list[str]] = {"보류": [], "신호 스킵": [], "미진입": []}
    noadd: list[str] = []
    for ln in log_lines:
        m = _RE_SKIP.search(ln)
        if m:
            buckets.setdefault(m.group(2), []).append(m.group(3))
            continue
        m = _RE_NOADD.search(ln)
        if m:
            noadd.append(f"{m.group(1)} · {m.group(2)}")
    any_hit = False
    for label, reasons in buckets.items():
        if not reasons:
            continue
        any_hit = True
        # 사유별 빈도 — 같은 사유가 반복되면 그게 곧 병목이다.
        # 가격·퍼센트 숫자는 N 으로 치환해야 같은 사유가 하나로 묶인다.
        freq: dict[str, int] = {}
        for r in reasons:
            key = _RE_NUM.sub("N", r.split("—")[0]).strip()[:70]
            freq[key] = freq.get(key, 0) + 1
        top = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:3]
        out.append(
            f"- {label} {len(reasons)}건 · 주요사유: "
            + " / ".join(f"{k}({c})" for k, c in top)
        )
    if not any_hit:
        out.append("- 신호 판정 로그 없음 (감시 미가동 또는 로그 미확보)")
    if noadd:
        out.append(f"- 섹터 미추가 {len(noadd)}건 (예: {noadd[0]})")
    return out, watched


# ────────────────────────────── 3. 체결 ──────────────────────────────
def _leader_trades(date: str) -> list[dict]:
    start = datetime.strptime(date, "%Y-%m-%d")
    end = start + timedelta(days=1)
    with Session(ENGINE) as s:
        rows = s.scalars(
            select(TradeLog)
            .where(TradeLog.ts >= start)
            .where(TradeLog.ts < end)
            .order_by(TradeLog.ts)
        ).all()
        out = []
        for r in rows:
            if r.strategy not in _LEADER_STRATEGIES:
                continue
            try:
                br = json.loads(r.broker_response) if r.broker_response else {}
            except Exception:
                br = {}
            if isinstance(br, dict) and br.get("dry_run"):
                continue
            kst = r.ts.replace(tzinfo=timezone.utc).astimezone(_KST)
            out.append({
                "ts": kst.strftime("%H:%M:%S"),
                "symbol": _bare(r.symbol),
                "side": (r.side or "").upper(),
                "price": float(r.price or 0),
                "strategy": r.strategy,
                "reason": r.reason or "",
            })
        return out


def _round_trips(trades: list[dict]) -> list[dict]:
    """종목별 BUY→SELL 순서쌍. 대장주는 종목당 1왕복이 정상이라 단순 큐로 충분."""
    pend: dict[str, dict] = {}
    rts: list[dict] = []
    for t in trades:
        sym = t["symbol"]
        if t["side"] == "BUY":
            pend[sym] = t
            continue
        b = pend.pop(sym, None)
        if not b:
            continue
        bp, sp = b["price"], t["price"]
        cost = bp * (1 + _BUY_COMM)
        net = ((sp * (1 - _SELL_COMM) - cost) / cost * 100) if cost else 0.0
        rts.append({
            "symbol": sym, "entry_ts": b["ts"], "exit_ts": t["ts"],
            "entry": bp, "exit": sp, "net_pct": net,
            "exit_reason": t["reason"][:80], "strategy": b["strategy"],
        })
    # 미청산(오버나이트)은 정상이 아니다 — 묻히지 않게 그대로 남긴다.
    for sym, b in pend.items():
        rts.append({
            "symbol": sym, "entry_ts": b["ts"], "exit_ts": "미청산",
            "entry": b["price"], "exit": 0.0, "net_pct": 0.0,
            "exit_reason": "미청산", "strategy": b["strategy"],
        })
    return rts


# ─────────────────────── 4. MFE/MAE · 반사실 ───────────────────────
def _hi_lo_after(bars: list[dict], hhmmss: str | None) -> tuple[float, float]:
    """hhmmss(HH:MM:SS) 이후 봉들의 (고가, 저가). None 이면 전체 구간."""
    cut = (hhmmss or "").replace(":", "")[:6]
    hi = lo = 0.0
    for b in bars:
        t = str(b.get("time") or "")
        if cut and t and t < cut:
            continue
        try:
            h = float(b.get("high") or 0)
            l = float(b.get("low") or 0)
        except Exception:
            continue
        if h:
            hi = max(hi, h)
        if l:
            lo = l if not lo else min(lo, l)
    return hi, lo


def _make_bar_fetcher(broker):
    """종목 -> 당일 3분봉. 같은 종목을 3·4·5단계가 나눠 쓰므로 캐시한다
    (KIS 유량 한도: 실전 18건/초 — 중복 조회는 그대로 낭비다)."""
    cache: dict[str, list[dict]] = {}

    def _bars(code: str) -> list[dict]:
        if code in cache:
            return cache[code]
        try:
            cache[code] = broker.get_minute_ohlcv_today(code, interval_min=3) or []
        except Exception as exc:
            logger.warning("leader_review: {} 분봉 조회 실패 {}", code, exc)
            cache[code] = []
        return cache[code]

    return _bars


def _stage_bars(_bars, round_trips: list[dict], watched: list[dict],
                max_counterfactual: int = 8) -> list[str]:
    """체결 MFE/MAE + 미진입 감시종목 반사실.

    `get_minute_ohlcv_today` 는 이름 그대로 **오늘** 분봉만 준다. 과거 날짜로
    리뷰를 돌리면 엉뚱한 날 봉이 붙으므로 호출부에서 당일일 때만 넘긴다.
    """
    fills = ["## 3. 체결 · MFE/MAE"]
    if not round_trips:
        fills.append("- 체결 0건")

    entered = set()
    for rt in round_trips:
        entered.add(rt["symbol"])
        e = rt["entry"] or 0
        hi, lo = _hi_lo_after(_bars(rt["symbol"]), rt["entry_ts"])
        mfe = (hi - e) / e * 100 if (e and hi) else 0.0
        mae = (lo - e) / e * 100 if (e and lo) else 0.0
        fills.append(
            f"- {rt['symbol']} {rt['entry_ts']}→{rt['exit_ts']} "
            f"진입 {e:,.0f} 청산 {rt['exit']:,.0f} · net {rt['net_pct']:+.2f}% "
            f"· MFE {mfe:+.2f}% / MAE {mae:+.2f}% · {rt['exit_reason']}"
        )
        if mfe >= 2.0 and rt["net_pct"] < 0.5:
            fills.append("    ↳ MFE 큼 + 실현 작음 = 청산 문제(진입 자체는 맞았다)")
        elif hi and mfe < 1.0:
            fills.append("    ↳ MFE 도 작음 = 진입/선정 문제(청산 탓이 아니다)")

    cf = ["## 4. 반사실 (미진입 감시종목 · 시가 대비 당일 고저)"]
    rest = [w for w in watched if w["code"] not in entered][:max_counterfactual]
    if not rest:
        cf.append("- 미진입 감시종목 없음")
    for w in rest:
        bars = _bars(w["code"])
        if not bars:
            continue
        base = 0.0
        for b in reversed(bars):   # newest-first → 가장 이른 봉의 시가가 기준
            try:
                base = float(b.get("open") or 0)
            except Exception:
                base = 0.0
            if base:
                break
        hi, lo = _hi_lo_after(bars, None)
        if not base or not hi:
            continue
        cf.append(
            f"- {w['name']}({w['code']}) {w['sector']}: "
            f"최대 {(hi - base) / base * 100:+.2f}% / 최저 {(lo - base) / base * 100:+.2f}%"
        )
    return fills + [""] + cf


# ───────────────── 5. 보유 중 섹터전환 반사실 ─────────────────
# 전환 1회 = 보유분 매도 + 후보 매수. 후보 매도는 어차피 청산 때 일어나 양쪽에
# 공통이므로 빼지 않는다(이중계상 방지).
_SWITCH_COST_PCT = (_BUY_COMM + _SELL_COMM) * 100


def _close_at(bars: list[dict], hhmmss: str | None) -> float:
    """hhmmss(HH:MM:SS) 시점까지의 마지막 종가. None 이면 그날 종가."""
    cut = (hhmmss or "").replace(":", "")[:6]
    best_t, best_c = "", 0.0
    for b in bars:
        t = str(b.get("time") or "")
        if not t or (cut and t > cut):
            continue
        if t >= best_t:
            try:
                c = float(b.get("close") or 0)
            except Exception:
                continue
            if c:
                best_t, best_c = t, c
    return best_c


def _entry_sector(date: str, code: str) -> str:
    """보유 종목이 어느 섹터로 편입됐는지 — 상태파일 바스켓에서 역추적."""
    st = _load_json(_STATE_DIR / f"{date}.json")
    for sec, members in (st.get("sector_baskets") or {}).items():
        for m in members or []:
            if _bare(m.get("code", "")) == code:
                return sec
    return ""


def _reval_snapshots(date: str) -> list[dict]:
    hist = _PICKS_DIR / f"{date}_reval_history.jsonl"
    if not hist.exists():
        return []
    out: list[dict] = []
    try:
        for ln in hist.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _stage_switch_cf(_bars, date: str, round_trips: list[dict],
                     max_points: int = 6) -> list[str]:
    """보유하는 동안 신1등 섹터로 갈아탔다면 어땠나(관측 전용).

    2026-08-24 이전 데이터로는 계산이 안 된다 — 그때까진 진입과 동시에 재선별이
    멈춰(leader_runner 슬롯 게이트) reval_history 가 진입 시점에서 끊겼다.
    """
    out = ["## 5. 보유 중 섹터전환 반사실"]
    snaps = _reval_snapshots(date)
    live = [r for r in round_trips if r.get("entry")]
    if not snaps or not live:
        out.append("- 재선별 스냅샷 또는 보유 이력 없음 — 계산 생략")
        return out

    edges: list[float] = []
    for rt in live:
        held, sec = rt["symbol"], (_entry_sector(date, rt["symbol"]) or "?")
        hb = _bars(held)
        open_ended = rt["exit_ts"] == "미청산"
        exit_cut = None if open_ended else rt["exit_ts"]
        held_exit = rt["exit"] or _close_at(hb, exit_cut)
        out.append(
            f"- 보유 {held} ({sec}) {rt['entry_ts']}->{rt['exit_ts']} "
            f"실제 net {rt['net_pct']:+.2f}%"
        )
        seen: set[str] = set()
        pts = 0
        for sn in snaps:
            t = str(sn.get("selected_at") or "")
            if not t or t <= rt["entry_ts"]:
                continue
            if not open_ended and t >= rt["exit_ts"]:
                continue
            secs = sn.get("sectors") or []
            if not secs:
                continue
            top = secs[0]
            cand = (top.get("top3") or [{}])[0]
            cc = _bare(cand.get("code", ""))
            if not cc or cc == held or cc in seen:
                continue   # 신1등이 보유 종목 자신이면 전환할 게 없다
            cb = _bars(cc)
            c_now, c_exit = _close_at(cb, t), _close_at(cb, exit_cut)
            h_now = _close_at(hb, t)
            if not (c_now and c_exit and h_now and held_exit):
                continue
            seen.add(cc)
            hold_ret = (held_exit - h_now) / h_now * 100
            sw_ret = (c_exit - c_now) / c_now * 100 - _SWITCH_COST_PCT
            edge = sw_ret - hold_ret
            edges.append(edge)
            held_score = next(
                (float(x.get("sector_score_100") or 0) for x in secs
                 if x.get("sector") == sec), 0.0)
            out.append(
                f"    {t[:5]} 보유섹터 {held_score:.1f}점 · 신1등 "
                f"{top.get('sector', '?')} {float(top.get('sector_score_100') or 0):.1f}점"
                f"({cand.get('name', '?')} {cc})"
            )
            out.append(
                f"      -> 전환 {sw_ret:+.2f}% vs 유지 {hold_ret:+.2f}% "
                f"= 우위 {edge:+.2f}%p (비용 {_SWITCH_COST_PCT:.2f}% 반영)"
            )
            pts += 1
            if pts >= max_points:
                break
        if not pts:
            out.append("    (전환 후보 없음 — 신1등이 보유 종목이거나 스냅샷 부족)")

    if edges:
        win = sum(1 for e in edges if e > 0)
        out.append(
            f"- 오늘 종합: 전환 우위 {win}/{len(edges)}회 · "
            f"평균 {sum(edges) / len(edges):+.2f}%p"
        )
        out.append(
            "  ※ 채택 문턱: 보유일 15일 이상 · 승률 60%↑ · 평균 우위 +0.9%p↑. "
            "하루치로 판단하지 마라."
        )
    return out


# ────────────────────────────── LLM ──────────────────────────────
def _llm_due(date: str) -> tuple[bool, str]:
    """LLM 해석을 돌릴 날인가? 금요일이거나, 직전 LLM 리뷰 이후 체결 N건 누적."""
    d = datetime.strptime(date, "%Y-%m-%d")
    need = max(1, int(getattr(_settings, "leader_review_llm_min_trades", 5)))
    if d.weekday() == 4:
        return True, "금요일 주간 해석"
    with Session(ENGINE) as s:
        rows = s.scalars(
            select(ReviewLog)
            .where(ReviewLog.kind == "leader")
            .where(ReviewLog.date <= date)
            .order_by(ReviewLog.date.desc())
            .limit(30)
        ).all()
        since = None
        for r in rows:
            try:
                if json.loads(r.raw_context or "{}").get("llm"):
                    since = r.date
                    break
            except Exception:
                continue
        start = datetime.strptime(since, "%Y-%m-%d") if since else d - timedelta(days=30)
        buys = s.scalars(
            select(TradeLog).where(TradeLog.ts >= start).where(TradeLog.ts < d + timedelta(days=1))
        ).all()
    n = sum(1 for r in buys
            if r.strategy in _LEADER_STRATEGIES and (r.side or "").upper() == "BUY")
    if n >= need:
        return True, f"미리뷰 체결 {n}건 누적(≥{need})"
    return False, f"표본 부족({n}/{need}) — 팩트시트만"


def _call_llm(date: str, facts: str) -> dict:
    from stock_bot.live.review import _llm_raw, _salvage_review_json

    prompt = (
        f"오늘({date} KST) 대장주봇 깔때기 팩트시트다.\n\n{facts}\n\n"
        "위 데이터만으로 평가하라. 없는 수치를 지어내지 마라."
    )
    raw = _llm_raw(prompt, _SYSTEM, source="leader_review")
    if raw is None:
        return {}
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return _salvage_review_json(raw)


# ────────────────────────────── 엔트리 ──────────────────────────────
def run_leader_review(date: str | None = None, broker=None) -> int | None:
    """대장주 깔때기 리뷰를 만들어 ReviewLog(kind='leader') 에 저장. row id 반환."""
    if not getattr(_settings, "leader_review_enabled", True):
        logger.info("leader review skip — LEADER_REVIEW_ENABLED=false")
        return None
    now_kst = datetime.now(tz=_KST)
    today = now_kst.strftime("%Y-%m-%d")
    date_str = date or today
    if date is None:
        from stock_bot.market_calendar import is_trading_day
        if not is_trading_day(now_kst):
            logger.info("leader review skip — {} 는 휴장일", date_str)
            return None

    sections: list[str] = []
    try:
        sections += _stage_settings() + [""]
    except Exception as exc:
        logger.warning("leader_review 설정단계 실패: {}", exc)
    try:
        sections += _stage_selection(date_str) + [""]
    except Exception as exc:
        logger.warning("leader_review 선별단계 실패: {}", exc)
    watched: list[dict] = []
    try:
        w_lines, watched = _stage_watch(date_str, _leader_log_lines(date_str))
        sections += w_lines + [""]
    except Exception as exc:
        logger.warning("leader_review 감시단계 실패: {}", exc)

    rts = _round_trips(_leader_trades(date_str))
    if date_str == today:
        # 분봉은 당일치만 조회 가능 — 과거 날짜 리뷰에선 이 단계를 건너뛴다.
        if broker is None:
            try:
                from stock_bot.broker import KISBroker
                broker = KISBroker()
            except Exception as exc:
                logger.warning("leader_review: 브로커 생성 실패 {} — 분봉 분석 생략", exc)
        if broker is not None:
            bars_fn = _make_bar_fetcher(broker)
            try:
                sections += _stage_bars(bars_fn, rts, watched)
            except Exception as exc:
                logger.warning("leader_review 분봉단계 실패: {}", exc)
            try:
                sections += [""] + _stage_switch_cf(bars_fn, date_str, rts)
            except Exception as exc:
                logger.warning("leader_review 전환반사실 실패: {}", exc)
    else:
        sections.append(f"## 3~5. 분봉 분석 생략 (과거 날짜 {date_str} — 당일 분봉만 조회 가능)")
    facts = "\n".join(sections).strip()

    due, why = _llm_due(date_str)
    result: dict = {}
    if due:
        try:
            result = _call_llm(date_str, facts) or {}
        except Exception as exc:
            logger.exception("leader_review LLM 실패: {}", exc)
    summary = str(result.get("summary", "")).strip() or f"{date_str} 대장주 팩트시트 ({why})"
    findings = result.get("findings") if isinstance(result.get("findings"), list) else []
    suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []

    rid = record_review(
        date=date_str,
        trades_count=len(rts),
        summary=summary,
        findings=findings,
        suggestions=suggestions,
        raw_context=json.dumps({"llm": bool(due and result), "facts": facts},
                               ensure_ascii=False),
        kind="leader",
    )
    logger.info("leader review 저장 id={} 왕복={} llm={} ({})",
                rid, len(rts), bool(due and result), why)

    lines = [f"🎯 **{date_str} 장마감 리뷰 · 대장주** (왕복 {len(rts)}건)", "", summary, "", facts]
    if findings:
        lines += ["", "**발견 사항**"] + [f"• {f}" for f in findings[:5]]
    if suggestions:
        lines += ["", "**제안 조정**"] + [f"• {s}" for s in suggestions[:5]]
    notify("\n".join(lines))
    return rid


if __name__ == "__main__":
    import sys
    run_leader_review(sys.argv[1] if len(sys.argv) > 1 else None)
