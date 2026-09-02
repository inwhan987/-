"""대장주봇 전용 장마감 리뷰 (스톡봇/앙상블 리뷰와 분리).

앙상블 리뷰(review.py)는 '체결 로그'가 곧 데이터지만, 대장주봇은 하루 체결이
0~1건이라 체결만 보면 볼 게 없다. 정보의 대부분은 **'왜 안 샀나'** 에 있다.
그래서 이 리뷰는 체결 리뷰가 아니라 **깔때기(funnel) 리뷰**로 만든다.

  1. 선별  — 오늘 섹터 순위/점수, 재선별로 1등 섹터가 바뀌었는지
  2. 감시  — 감시 바스켓, 보류/신호스킵/미진입 건수와 사유 분포
  3. 체결  — 진입·청산 왕복, net%, **MFE/MAE**
  4. 반사실 — 막힌 신호를 그 시각에 샀다면 익절/손절 중 어디에 먼저 닿았나
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
from stock_bot.names import get_name
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
- 4단계 반사실은 **막힌 신호를 그 시각에 샀다고 가정한 익절/손절 선도달**
  집계다. 사유별·눌림구간별 승률을 보고 답하라: 승률이 높은 구간이 남아
  있으면 그 사유의 임계 파라미터(0단계에 현재값이 있다)가 과보수적이다.
  전 구간 승률이 낮으면 컷이 옳았고 병목은 선별·감시 쪽이다.
- **반드시 답해야 할 두 가지**: (1) 오늘 진입이 막힌 지배적 사유는 무엇이고
  깔때기 어느 단계인가, (2) 어떤 파라미터를 현재값에서 어느 값으로 바꿨다면
  결과가 달라졌겠나 — 4단계 승률로 뒷받침되지 않으면 '근거 부족'이라 써라.

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


_NAME_CACHE: dict[str, str] = {}


def _disp(code: str, name: str = "") -> str:
    """리포트용 '005930 삼성전자'. 코드만 찍으면 읽는 사람이 어느 종목인지
    모른다(2026-09-02 피드백). 로그에서 이미 이름을 뜯어온 경우 그걸 쓰고,
    없을 때만 get_name 조회 — 조회는 프로세스 캐시에 남긴다."""
    bc = _bare(code)
    if not bc:
        return str(code) or "?"
    nm = (name or "").strip()
    if not nm:
        if bc not in _NAME_CACHE:
            try:
                _NAME_CACHE[bc] = get_name(bc) or ""
            except Exception:
                _NAME_CACHE[bc] = ""
        nm = _NAME_CACHE[bc]
    return f"{bc} {nm}" if nm and nm != bc else bc


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


_RE_TS = re.compile(r"^\d{4}-\d{2}-\d{2} (\d{2}):(\d{2}):(\d{2})")
_RE_PULL = re.compile(r"대비 ([-+][0-9.]+)%")


def _hms_to_sec(hms: str) -> int:
    """HHMMSS 또는 HH:MM:SS -> 자정 이후 초."""
    d = hms.replace(":", "")
    if len(d) == 4:          # HHMM 로 오는 피드도 있어 초를 채운다
        d += "00"
    if len(d) < 6:
        return -1
    try:
        return int(d[:2]) * 3600 + int(d[2:4]) * 60 + int(d[4:6])
    except Exception:
        return -1


def _skip_events(log_lines: list[str]) -> list[dict]:
    """미진입·보류·신호스킵 로그를 (시각·종목·사유·눌림폭) 로 구조화.

    반사실을 '시가 대비'로 재던 게 이 리뷰의 가장 큰 결함이었다. 감시는 09:30
    부터인데 09:00 시가를 기준으로 삼으면 선별 전 상승이 통째로 '놓친 폭'으로
    잡힌다(08-26 한전산업 +29.33% — 선별 시점에 이미 +10% 오른 상태였다).
    그 숫자로는 '샀어야 했나'에 답할 수 없다. 답이 되는 건 하나뿐이다:
    **신호가 막힌 그 시각에 샀다면 익절선과 손절선 중 어디에 먼저 닿았나.**
    """
    out: list[dict] = []
    for ln in log_lines:
        mt = _RE_TS.match(ln)
        if not mt:
            continue
        m = _RE_SKIP.search(ln)
        if not m:
            continue
        who = m.group(1).split()
        code = _bare(who[0]) if who else ""
        if not code.isdigit():
            continue
        reason = m.group(3)
        # 사유 종류 — 파라미터 한 개에 대응시켜야 '뭘 조정했어야 했나'가 나온다.
        if "붕괴컷" in reason:
            kind = "붕괴컷"
        elif "되받음 미충족" in reason:
            kind = "되받음 미충족"
        elif "장대양봉컷" in reason:
            kind = "장대양봉컷"
        elif "기울기컷" in reason:
            kind = "기울기컷"
        else:
            kind = reason.split("(")[0].strip()[:24] or "기타"
        mp = _RE_PULL.search(reason)
        out.append({
            "sec": int(mt.group(1)) * 3600 + int(mt.group(2)) * 60 + int(mt.group(3)),
            "hm": f"{mt.group(1)}:{mt.group(2)}",
            "code": code,
            "name": who[1] if len(who) > 1 else "",
            "kind": kind,
            "pull": float(mp.group(1)) if mp else None,
        })
    return out


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
    # 사유 집계만 있으면 '3건 막혔다'는 알아도 뭐가 막혔는지는 모른다.
    subjects: dict[str, list[str]] = {}
    noadd: list[str] = []
    for ln in log_lines:
        m = _RE_SKIP.search(ln)
        if m:
            buckets.setdefault(m.group(2), []).append(m.group(3))
            who = m.group(1).split()
            if who:
                subjects.setdefault(m.group(2), []).append(
                    _disp(who[0], who[1] if len(who) > 1 else ""))
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
        uniq = list(dict.fromkeys(subjects.get(label) or []))
        if uniq:
            more = f" 외 {len(uniq) - 6}종목" if len(uniq) > 6 else ""
            out.append(f"    · 대상: {' / '.join(uniq[:6])}{more}")
    if not any_hit:
        out.append("- 신호 판정 로그 없음 (감시 미가동 또는 로그 미확보)")
    if noadd:
        out.append(f"- 섹터 미추가 {len(noadd)}건 (예: {noadd[0]})")
    return out, watched


# ────────────────────────────── 3. 체결 ──────────────────────────────
def _leader_trades(date: str, since: str | None = None) -> list[dict]:
    """대장주 체결. since 를 주면 [since, date] 구간(누적 리뷰용), 없으면 당일."""
    start = datetime.strptime(since or date, "%Y-%m-%d")
    end = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
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
                "date": kst.strftime("%Y-%m-%d"),
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
            "date": b.get("date", ""),
            "symbol": sym, "entry_ts": b["ts"], "exit_ts": t["ts"],
            "entry": bp, "exit": sp, "net_pct": net,
            "exit_reason": t["reason"][:80], "strategy": b["strategy"],
            "entry_reason": (b.get("reason") or "")[:90],
        })
    # 미청산(오버나이트)은 정상이 아니다 — 묻히지 않게 그대로 남긴다.
    for sym, b in pend.items():
        rts.append({
            "date": b.get("date", ""),
            "symbol": sym, "entry_ts": b["ts"], "exit_ts": "미청산",
            "entry": b["price"], "exit": 0.0, "net_pct": 0.0,
            "exit_reason": "미청산", "strategy": b["strategy"],
            "entry_reason": (b.get("reason") or "")[:90],
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
                max_counterfactual: int = 8, date: str = "") -> list[str]:
    """체결 MFE/MAE.

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
        e_sec, x_sec = _hms_to_sec(rt["entry_ts"]), _hms_to_sec(rt["exit_ts"])
        held = (f" · 보유 {int((x_sec - e_sec) / 60)}분"
                if e_sec >= 0 and x_sec >= e_sec else "")
        fills.append(
            f"- {_disp(rt['symbol'])} {rt['entry_ts']}→{rt['exit_ts']}{held} "
            f"진입 {e:,.0f} 청산 {rt['exit']:,.0f} · net {rt['net_pct']:+.2f}% "
            f"· MFE {mfe:+.2f}% / MAE {mae:+.2f}% · {rt['exit_reason']}"
        )
        # (A) 왜 샀나 — 섹터·바스켓 순위·선별 시 등락·점수·진입 신호.
        meta = _entry_meta(date, rt["symbol"]) if date else {}
        why = []
        if meta.get("sector"):
            why.append(f"{meta['sector']} 섹터")
        if meta.get("rank") is not None:
            why.append(f"바스켓 {meta['rank']}위")
        if meta.get("change_pct") is not None:
            try:
                why.append(f"선별시 {float(meta['change_pct']):+.2f}%")
            except Exception:
                pass
        if meta.get("stock_score") is not None:
            try:
                why.append(f"종목점수 {float(meta['stock_score']):.0f}")
            except Exception:
                pass
        if rt.get("entry_reason"):
            why.append(rt["entry_reason"])
        if why:
            fills.append("    ↳ 근거: " + " · ".join(why))

        # (B) 판 뒤엔 어떻게 됐나 — 익절이 이르지 않았는지, 손절이 옳았는지.
        if rt["exit_ts"] != "미청산" and rt.get("exit"):
            xb = _bars(rt["symbol"])
            a_hi, a_lo = _hi_lo_after(xb, rt["exit_ts"])
            eod = _close_at(xb, None)
            xp = rt["exit"]
            if xp and (a_hi or eod):
                pieces = []
                if a_hi:
                    pieces.append(f"이후 최고 {a_hi:,.0f} ({(a_hi - xp) / xp * 100:+.2f}%)")
                if a_lo:
                    pieces.append(f"최저 {a_lo:,.0f} ({(a_lo - xp) / xp * 100:+.2f}%)")
                if eod:
                    pieces.append(f"종가 {eod:,.0f} ({(eod - xp) / xp * 100:+.2f}%)")
                fills.append("    ↳ 청산 후: " + " · ".join(pieces))
                # 청산 판단을 한 줄로 채점한다. 기준은 '그대로 들고 종가까지'.
                hold = (eod - xp) / xp * 100 if eod else 0.0
                if a_hi and (a_hi - xp) / xp * 100 >= 2.0 and hold > 0.5:
                    fills.append("        = 너무 일찍 팔았다(익절선이 낮거나 트레일이 없다)")
                elif eod and hold < -1.0:
                    fills.append("        = 청산이 옳았다(들고 있었으면 더 나빴다)")

        if mfe >= 2.0 and rt["net_pct"] < 0.5:
            fills.append("    ↳ MFE 큼 + 실현 작음 = 청산 문제(진입 자체는 맞았다)")
        elif hi and mfe < 1.0:
            fills.append("    ↳ MFE 도 작음 = 진입/선정 문제(청산 탓이 아니다)")

    return fills


# ─────────── 4. 반사실: 막힌 신호를 샀다면 어떻게 됐나 ───────────
def _outcome_after(bars_asc: list[dict], i0: int, entry: float,
                   tp: float, stop: float) -> str:
    """i0 다음 봉부터 훑어 익절선·손절선 중 먼저 닿는 쪽. 둘 다 아니면 '미결'.

    한 봉 안에서 고가·저가가 둘 다 닿으면 손절로 센다(보수적). 봉 내부 순서는
    분봉으로 알 수 없고, 낙관적으로 세면 컷을 푸는 쪽으로 결론이 기운다.
    """
    for b in bars_asc[i0 + 1:]:
        try:
            hi, lo = float(b.get("high") or 0), float(b.get("low") or 0)
        except Exception:
            continue
        if lo and lo <= stop:
            return "손절"
        if hi and hi >= tp:
            return "익절"
    return "미결"


def _stage_missed(_bars, events: list[dict], interval_min: int) -> list[str]:
    """막힌 신호마다 '그 시각에 샀다면'을 되짚어 사유별로 집계한다.

    진입 가정은 실제 로직과 같게 잡는다 — 진입가=확정봉 종가, 손절=확정봉
    저가×(1-stop_buf%), 익절=진입가×(1+tp%). exit_mode 가 split 이면 1차
    익절선이 실질 목표라 그 값을 쓴다.
    """
    g = lambda k, d=0: getattr(_settings, k, d)
    ex = str(g("leader_exit_mode", "fixed"))
    tp_pct = float({
        "split": g("leader_split_tp1_pct", 2.0),
        "trail": g("leader_trail_activate_pct", 4.0),
    }.get(ex, g("leader_tp_pct", 4.0)))
    stop_pct = float(g("leader_stop_buf_pct", 1.5))

    out = [
        f"## 4. 반사실 — 막힌 신호를 그 시각에 샀다면 "
        f"(익절 +{tp_pct:.1f}% / 손절 참조저점 -{stop_pct:.1f}%, 동시터치는 손절)"
    ]
    if not events:
        out.append("- 신호판정 이벤트 없음")
        return out

    bar_sec = max(1, int(interval_min)) * 60
    rows: list[dict] = []
    for ev in events:
        bars = _bars(ev["code"])
        if not bars:
            continue
        asc = list(reversed(bars))    # 조회는 newest-first
        # 로그 시각에 '이미 닫혀 있던' 마지막 봉 = 판정에 쓰인 확정봉.
        i0 = -1
        for i, b in enumerate(asc):
            t = _hms_to_sec(str(b.get("time") or ""))
            if t >= 0 and t + bar_sec <= ev["sec"]:
                i0 = i
            else:
                break
        if i0 < 0 or i0 + 1 >= len(asc):
            continue
        try:
            entry = float(asc[i0].get("close") or 0)
            ref = float(asc[i0].get("low") or 0)
        except Exception:
            continue
        if not entry or not ref:
            continue
        res = _outcome_after(asc, i0, entry,
                             entry * (1 + tp_pct / 100), ref * (1 - stop_pct / 100))
        rows.append({**ev, "res": res})

    if not rows:
        out.append(f"- 이벤트 {len(events)}건 · 분봉 매칭 0건 (분봉 미확보)")
        return out

    def _tally(rs: list[dict]) -> str:
        w = sum(1 for r in rs if r["res"] == "익절")
        l = sum(1 for r in rs if r["res"] == "손절")
        u = sum(1 for r in rs if r["res"] == "미결")
        dec = w + l
        rate = f" · 승률 {w / dec * 100:.0f}%" if dec else ""
        return f"익절 {w} · 손절 {l} · 미결 {u}{rate}"

    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    for kind, rs in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        out.append(f"- **{kind}** {len(rs)}건 → {_tally(rs)}")
        # 집계만으로는 어느 종목이 몇 시에 막혔는지 알 수 없다. 건별로 깐다.
        for r in sorted(rs, key=lambda x: x["sec"])[:6]:
            pull = (f" · 눌림 {r['pull']:+.2f}%"
                    if r.get("pull") is not None else "")
            out.append(
                f"    · {r['hm']} {_disp(r['code'], r.get('name', ''))}"
                f" → {r['res']}{pull}"
            )
        if len(rs) > 6:
            out.append(f"    · … 외 {len(rs) - 6}건")
        # 붕괴컷처럼 임계값이 있는 사유는 구간별로 쪼갠다. 어느 구간부터
        # 승률이 무너지는지가 곧 '그 손잡이를 어디까지 풀어야 하나'의 답이다.
        pulls = [r for r in rs if r.get("pull") is not None]
        if len(pulls) >= 4:
            bands = [(0.0, 3.0), (3.0, 4.0), (4.0, 5.0), (5.0, 99.0)]
            for lo_b, hi_b in bands:
                sub = [r for r in pulls if lo_b <= abs(r["pull"]) < hi_b]
                if sub:
                    out.append(f"    · 눌림 -{lo_b:.0f}~-{hi_b:.0f}% {len(sub)}건: {_tally(sub)}")
    out.append(
        "  ※ 승률이 높은 구간이 남아 있으면 그 사유의 임계 파라미터가 과보수적이다."
        " 전 구간 승률이 낮으면 컷이 옳았고 문제는 선별·감시 쪽이다."
    )
    return out


# ────────── 5. 선별됐지만 감시 밖으로 빠진 후보 반사실 ──────────
def _stage_dropped(_bars, date: str, interval_min: int,
                   max_rows: int = 10) -> list[str]:
    """정본 picks 의 top3 중 실제 바스켓에 못 든 종목을 그날 성과로 되짚는다.

    4단계는 '감시는 했는데 신호에서 막힌' 종목만 본다. 그보다 앞단 —
    선별에서는 뽑혔는데 60%룰·섹터 상한에 잘려 아예 감시조차 안 한 종목 —
    은 리포트 어디에도 안 나왔다. 그쪽이 더 좋았다면 문제는 신호가 아니라
    바스켓 컷이다.

    ※ 진입 가정이 4단계와 다르다. 이 종목들은 감시를 안 했으니 신호 시각이
      없다. 그래서 '당일 첫 3분봉 종가에 사서 같은 익절/손절 규칙을 적용'
      한 buy&hold 근사다 — 실제 전략(눌림목 진입)보다 낙관적일 수 있어
      절대 수치가 아니라 감시한 종목과의 상대 비교로만 읽어야 한다.
    """
    out = ["## 5. 반사실 — 선별됐지만 감시 밖으로 빠진 후보"]
    base = _load_json(_PICKS_DIR / f"{date}.json")
    leaders = (base.get("leaders") or []) if base else []
    if not leaders:
        out.append("- 정본 picks 없음 — 계산 생략")
        return out
    st = _load_json(_STATE_DIR / f"{date}.json")
    baskets = st.get("sector_baskets") or {}
    if not baskets:
        out.append("- 바스켓 스냅샷 없음 (매매 미기동?) — 계산 생략")
        return out
    watched_codes = {
        _bare(m.get("code", ""))
        for members in baskets.values() for m in (members or [])
    }
    own = {_bare(x) for x in (getattr(_settings, "symbols", []) or [])}
    ratio = float(getattr(_settings, "leader_band_ratio", 0.6))

    cands: list[dict] = []
    for L in leaders:
        sec = L.get("sector", "?")
        top3 = sorted(L.get("top3") or [], key=lambda x: x.get("rank", 9))
        if not top3:
            continue
        try:
            lead_sc = float(top3[0].get("stock_score", 0) or 0)
        except Exception:
            lead_sc = 0.0
        for m in top3:
            code = _bare(m.get("code", ""))
            if not code or code in watched_codes:
                continue
            try:
                sc = float(m.get("stock_score", 0) or 0)
            except Exception:
                sc = 0.0
            if sec not in baskets:
                cut = "섹터 미채택"
            elif code in own:
                cut = "스톡봇 중복"
            elif lead_sc > 0 and sc < lead_sc * ratio:
                cut = f"점수컷({ratio:.0%}룰)"
            else:
                cut = "바스켓 제외"
            cands.append({"code": code, "name": m.get("name", ""),
                          "sector": sec, "rank": m.get("rank"),
                          "score": sc, "cut": cut})
    if not cands:
        out.append("- 탈락 후보 없음 (선별 top3 가 전부 감시에 들어갔다)")
        return out

    g = lambda k, d=0: getattr(_settings, k, d)
    ex = str(g("leader_exit_mode", "fixed"))
    tp_pct = float({
        "split": g("leader_split_tp1_pct", 2.0),
        "trail": g("leader_trail_activate_pct", 4.0),
    }.get(ex, g("leader_tp_pct", 4.0)))
    stop_pct = float(g("leader_stop_buf_pct", 1.5))
    out[0] += f" (첫 봉 매수 가정 · 익절 +{tp_pct:.1f}% / 손절 첫봉저점 -{stop_pct:.1f}%)"

    rows: list[dict] = []
    for c in cands:
        bars = _bars(c["code"])
        if not bars:
            continue
        asc = list(reversed(bars))
        if len(asc) < 2:
            continue
        try:
            entry = float(asc[0].get("close") or 0)
            ref = float(asc[0].get("low") or 0)
        except Exception:
            continue
        if not entry or not ref:
            continue
        res = _outcome_after(asc, 0, entry,
                             entry * (1 + tp_pct / 100), ref * (1 - stop_pct / 100))
        hi = max((float(b.get("high") or 0) for b in asc), default=0.0)
        eod = float(asc[-1].get("close") or 0)
        rows.append({**c, "entry": entry, "res": res,
                     "mfe": (hi - entry) / entry * 100 if hi else 0.0,
                     "eod": (eod - entry) / entry * 100 if eod else 0.0})
    if not rows:
        out.append(f"- 탈락 후보 {len(cands)}종목 · 분봉 매칭 0건 (분봉 미확보)")
        return out

    w = sum(1 for r in rows if r["res"] == "익절")
    l = sum(1 for r in rows if r["res"] == "손절")
    dec = w + l
    rate = f" · 승률 {w / dec * 100:.0f}%" if dec else ""
    avg = sum(r["eod"] for r in rows) / len(rows)
    out.append(
        f"- 탈락 {len(cands)}종목 중 {len(rows)}종목 판정 → 익절 {w} · 손절 {l}"
        f" · 미결 {len(rows) - dec}{rate} · 종가등락 평균 {avg:+.2f}%"
    )
    for r in sorted(rows, key=lambda x: -x["mfe"])[:max_rows]:
        rk = f"{r['rank']}위 " if r.get("rank") is not None else ""
        out.append(
            f"    · {_disp(r['code'], r['name'])} ({r['sector']} {rk}점수 {r['score']:.0f})"
            f" · {r['cut']} → {r['res']} · 고점 {r['mfe']:+.2f}% / 종가 {r['eod']:+.2f}%"
        )
    if len(rows) > max_rows:
        out.append(f"    · … 외 {len(rows) - max_rows}종목")
    by_cut: dict[str, list[dict]] = {}
    for r in rows:
        by_cut.setdefault(r["cut"], []).append(r)
    if len(by_cut) > 1:
        out.append("  · 사유별 종가등락 평균: " + " / ".join(
            f"{k} {sum(x['eod'] for x in v) / len(v):+.2f}%({len(v)})"
            for k, v in sorted(by_cut.items(), key=lambda kv: -len(kv[1]))
        ))
    out.append(
        "  ※ 감시한 종목보다 이쪽 성과가 꾸준히 좋으면 바스켓 컷(60%룰·섹터 상한)이"
        " 잘못 자르고 있다는 뜻이다. 반대면 컷이 제 일을 한 것이다."
    )
    return out


# ───────────────── 6. 보유 중 섹터전환 반사실 ─────────────────
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


def _entry_meta(date: str, code: str) -> dict:
    """진입 종목의 선별 이력 — 어느 섹터로, 바스켓 몇 위로, 어떤 점수로 들어왔나.

    '왜 이 종목을 샀나'가 리포트에 없어서 체결만 보고는 선별이 맞았는지
    판단할 수 없었다(2026-09-02 피드백). 상태파일 바스켓 스냅샷에서 역추적한다.
    """
    st = _load_json(_STATE_DIR / f"{date}.json")
    for sec, members in (st.get("sector_baskets") or {}).items():
        for m in members or []:
            if _bare(m.get("code", "")) == code:
                return {
                    "sector": sec,
                    "rank": m.get("rank"),
                    "change_pct": m.get("change_pct"),
                    "stock_score": m.get("stock_score"),
                    "name": m.get("name", ""),
                }
    return {}


def _entry_sector(date: str, code: str) -> str:
    return str(_entry_meta(date, code).get("sector") or "")


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


def _stage_window(since: str | None, date: str) -> list[str]:
    """직전 LLM 리뷰 이후 누적 체결 — LLM 이 도는 날에만 붙인다.

    1~6단계가 전부 '하루 창'이라 금요일 주간 해석도 실제로는 그날 하루만 봤다.
    대장주는 하루 최대 1왕복(leader_max_positions=1)이고 첫 매수 순간 스캔이
    멈추므로, 산 날엔 신호 게이트(≥15)가 구조적으로 안 열린다. 그 결과
    2026-08-24 삼성SDI · 08-27 HD현대일렉 체결이 어떤 LLM 리뷰에도 안 들어갔다.
    분봉은 당일치만 조회되니 3~5단계는 못 넓히지만, 체결 자체(진입·청산·사유·
    net)는 DB 에 있다. 손익비를 보려면 결국 이 표가 있어야 한다.
    """
    label = f"{since} 이후" if since else "최근 30일"
    out = [f"## 7. 누적 체결 ({label} ~ {date})"]
    rts = _round_trips(_leader_trades(date, since=since))
    if not rts:
        out.append("- 구간 내 왕복 없음")
        return out
    out.append("| 날짜 | 종목 | 진입 | 청산 | net% | 청산사유 |")
    out.append("|---|---|---|---|---|---|")
    for r in rts:
        out.append(
            f"| {r.get('date', '')} | {_disp(r['symbol'])} | {r['entry']:,.0f} | "
            f"{r['exit']:,.0f} | {r['net_pct']:+.2f} | {r['exit_reason']} |"
        )
    closed = [r for r in rts if r["exit_ts"] != "미청산"]
    if not closed:
        return out
    wins = [r["net_pct"] for r in closed if r["net_pct"] > 0]
    losses = [r["net_pct"] for r in closed if r["net_pct"] <= 0]
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    wr = len(wins) / len(closed) * 100
    # 손익비 R = 평균이익 / |평균손실|. 손실이 없으면 계산 불가(표본 부족).
    rr = (avg_w / abs(avg_l)) if avg_l else 0.0
    exp = sum(r["net_pct"] for r in closed) / len(closed)
    # 이 승률에서 본전이 되려면 필요한 R, 그리고 이 R 에서 필요한 승률.
    be_wr = (1 / (1 + rr) * 100) if rr else 0.0
    out.append("")
    out.append(
        f"- 왕복 {len(closed)}건 · 승 {len(wins)} / 패 {len(losses)} · 승률 {wr:.1f}%"
    )
    out.append(
        f"- 평균이익 {avg_w:+.2f}% · 평균손실 {avg_l:+.2f}% · "
        f"손익비 R {rr:.2f} · 기대값 {exp:+.2f}%/건"
    )
    if rr:
        out.append(f"- 이 R 의 손익분기 승률 {be_wr:.1f}% (현재 {wr:.1f}%)")
    # 청산사유 분포 — 익절 상단이 잘리는지(마감청산 비중) 보는 핵심 지표다.
    freq: dict[str, int] = {}
    for r in closed:
        key = _RE_NUM.sub("N", r["exit_reason"]).strip()[:40] or "?"
        freq[key] = freq.get(key, 0) + 1
    dist = " · ".join(f"{k} {v}건" for k, v in
                      sorted(freq.items(), key=lambda kv: kv[1], reverse=True))
    out.append(f"- 청산사유 분포: {dist}")
    return out


def _stage_switch_cf(_bars, date: str, round_trips: list[dict],
                     max_points: int = 6) -> list[str]:
    """보유하는 동안 신1등 섹터로 갈아탔다면 어땠나(관측 전용).

    2026-08-24 이전 데이터로는 계산이 안 된다 — 그때까진 진입과 동시에 재선별이
    멈춰(leader_runner 슬롯 게이트) reval_history 가 진입 시점에서 끊겼다.
    """
    out = ["## 6. 보유 중 섹터전환 반사실"]
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
            f"- 보유 {_disp(held)} ({sec}) {rt['entry_ts']}->{rt['exit_ts']} "
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
def _last_llm_date(date: str) -> str | None:
    """직전에 LLM 해석까지 돈 대장주 리뷰 날짜. 없으면 None."""
    with Session(ENGINE) as s:
        rows = s.scalars(
            select(ReviewLog)
            .where(ReviewLog.kind == "leader")
            .where(ReviewLog.date < date)
            .order_by(ReviewLog.date.desc())
            .limit(30)
        ).all()
    for r in rows:
        try:
            if json.loads(r.raw_context or "{}").get("llm"):
                return r.date
        except Exception:
            continue
    return None


def _llm_due(date: str, signals: int = 0) -> tuple[bool, str, str | None]:
    """LLM 해석을 돌릴 날인가?

    금요일이거나 · 직전 LLM 리뷰 이후 체결 N건 누적이거나 · **당일 신호판정이
    M건 이상**이면 실행한다. 세 번째 조건이 2026-08-26 에 추가됐다: 그날 감시
    41건이 전부 붕괴컷에 막혔는데도 체결이 0이라 '표본 부족(0/5)'으로 넘어갔다.
    이 리뷰의 질문은 '왜 안 샀나'인데 표본을 체결 건수로만 세면, 정작 답이
    가장 많이 쌓인 날에 해석을 건너뛰게 된다.

    Returns: (실행여부, 사유, 누적구간 시작일) — 세 번째 값은 6단계(누적 체결)
    가 쓴다. 직전 LLM 리뷰 **다음 날**부터 센다(그날 체결은 그 리뷰가 이미 봤다).
    """
    d = datetime.strptime(date, "%Y-%m-%d")
    need = max(1, int(getattr(_settings, "leader_review_llm_min_trades", 5)))
    need_sig = int(getattr(_settings, "leader_review_llm_min_signals", 15))
    prev = _last_llm_date(date)
    if prev:
        start = datetime.strptime(prev, "%Y-%m-%d") + timedelta(days=1)
    else:
        start = d - timedelta(days=30)
    since = start.strftime("%Y-%m-%d")
    if d.weekday() == 4:
        return True, "금요일 주간 해석", since
    if need_sig > 0 and signals >= need_sig:
        return True, f"당일 신호판정 {signals}건(≥{need_sig})", since
    with Session(ENGINE) as s:
        buys = s.scalars(
            select(TradeLog).where(TradeLog.ts >= start).where(TradeLog.ts < d + timedelta(days=1))
        ).all()
    n = sum(1 for r in buys
            if r.strategy in _LEADER_STRATEGIES and (r.side or "").upper() == "BUY")
    if n >= need:
        return True, f"미리뷰 체결 {n}건 누적(≥{need})", since
    return False, (f"표본 부족 — 미리뷰 체결 {n}/{need} · 당일 신호판정 "
                   f"{signals}/{need_sig} — 팩트시트만"), since


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
    events: list[dict] = []
    log_lines = _leader_log_lines(date_str)
    try:
        w_lines, watched = _stage_watch(date_str, log_lines)
        sections += w_lines + [""]
    except Exception as exc:
        logger.warning("leader_review 감시단계 실패: {}", exc)
    try:
        events = _skip_events(log_lines)
    except Exception as exc:
        logger.warning("leader_review 신호이벤트 파싱 실패: {}", exc)

    # LLM 게이트를 먼저 판정한다 — 도는 날에만 7단계(누적 체결)를 붙인다.
    due, why, since = False, "", None
    try:
        due, why, since = _llm_due(date_str, len(events))
    except Exception as exc:
        logger.warning("leader_review LLM 게이트 판정 실패: {}", exc)
        why = "게이트 판정 실패 — 팩트시트만"

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
                sections += _stage_bars(bars_fn, rts, watched, date=date_str)
            except Exception as exc:
                logger.warning("leader_review 분봉단계 실패: {}", exc)
            try:
                sections += [""] + _stage_missed(
                    bars_fn, events,
                    int(getattr(_settings, "leader_interval_min", 3)))
            except Exception as exc:
                logger.warning("leader_review 미진입반사실 실패: {}", exc)
            try:
                sections += [""] + _stage_dropped(
                    bars_fn, date_str,
                    int(getattr(_settings, "leader_interval_min", 3)))
            except Exception as exc:
                logger.warning("leader_review 탈락후보반사실 실패: {}", exc)
            try:
                sections += [""] + _stage_switch_cf(bars_fn, date_str, rts)
            except Exception as exc:
                logger.warning("leader_review 전환반사실 실패: {}", exc)
    else:
        sections.append(f"## 3~6. 분봉 분석 생략 (과거 날짜 {date_str} — 당일 분봉만 조회 가능)")
    if due:
        # 1~6단계는 전부 '당일 창'이다(분봉을 당일치만 조회할 수 있어 넓힐 수
        # 없다). LLM 이 도는 날엔 직전 리뷰 이후 체결을 표로 덧붙여야 '금요일
        # 주간 해석'이 실제로 주간이 된다 — 안 그러면 산 날의 체결이 통째로
        # 새어나간다(2026-08-24 삼성SDI · 08-27 HD현대일렉).
        try:
            sections += [""] + _stage_window(since, date_str)
        except Exception as exc:
            logger.warning("leader_review 누적체결단계 실패: {}", exc)
    facts = "\n".join(sections).strip()

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
