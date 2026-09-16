# -*- coding: utf-8 -*-
"""SQLite 저장소 — `data/swing.db` (명세 3-1).

명세의 6개 테이블(daily/meta/watchlist/signals/positions/bars)에 더해 수급·밸류·
프로그램매매를 따로 둔다(flow/fund/program). 일봉과 주기·소스가 달라 같은 행에
넣으면 부분 갱신 때 서로 덮어쓴다.

positions 는 반드시 DB 에 있어야 한다 — 프로세스가 죽어도 재시작 시 손절이 돼야
한다. signals 는 모드와 무관하게 항상 쓴다.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pandas as pd

from .config import cfg

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    code TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER, value INTEGER, updated TEXT,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date);

CREATE TABLE IF NOT EXISTS meta (
    code TEXT PRIMARY KEY, name TEXT, market TEXT,
    mktcap INTEGER, shares INTEGER, updated TEXT
);

-- 투자자별 순매수 거래대금(원). pykrx flow() / KIS inquire-investor *_ntby_tr_pbmn 과 같은 뜻.
CREATE TABLE IF NOT EXISTS flow (
    code TEXT NOT NULL, date TEXT NOT NULL,
    forgn REAL, inst REAL, indiv REAL, updated TEXT,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_flow_date ON flow(date);

-- PER/PBR/EPS. pykrx fundamental() (KRX 공표치) / KIS inquire-price 당일치.
CREATE TABLE IF NOT EXISTS fund (
    code TEXT NOT NULL, date TEXT NOT NULL,
    per REAL, pbr REAL, eps REAL, updated TEXT,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_fund_date ON fund(date);

-- 프로그램매매 전체 순매수(수량·대금). 지금은 수집만(명세 15절).
CREATE TABLE IF NOT EXISTS program (
    code TEXT NOT NULL, date TEXT NOT NULL,
    ntby_qty REAL, ntby_value REAL, updated TEXT,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS watchlist (
    date TEXT, code TEXT, strategy TEXT,
    score REAL, rank_in_strategy INTEGER, rank_overall INTEGER,
    ref_ma20 REAL, ref_ma60 REAL, ref_prev_close REAL,
    ref_box_top REAL, ref_atr_pct REAL, ref_avg_bar_vol REAL,
    stop_px REAL, tp_px REAL,
    subscribed INTEGER,
    ref_value_ma20 REAL, ref_mktcap REAL, pscore REAL,
    PRIMARY KEY (date, code, strategy)
);

CREATE TABLE IF NOT EXISTS signals (
    date TEXT, code TEXT, strategy TEXT,
    score REAL, rank_overall INTEGER,
    trigger_time TEXT,
    trigger_px REAL,
    trigger_reason TEXT,
    no_trigger_reason TEXT,
    day_open REAL, day_high REAL, day_low REAL, day_close REAL,
    fwd1 REAL, fwd3 REAL, fwd5 REAL,
    watched INTEGER, pscore REAL, trade_date TEXT,
    PRIMARY KEY (date, code, strategy)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT,
    code TEXT, strategy TEXT, state TEXT,
    entry_date TEXT, entry_time TEXT, entry_px REAL, shares INTEGER,
    stop_px REAL, tp_px REAL, peak REAL, trail_on INTEGER,
    exit_date TEXT, exit_time TEXT, exit_px REAL, exit_reason TEXT,
    order_no TEXT, signal_date TEXT, note TEXT,
    fwd1 REAL, fwd3 REAL, fwd5 REAL,
    updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(mode, state);

CREATE TABLE IF NOT EXISTS bars (
    code TEXT, date TEXT, bar_key TEXT,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER, ticks INTEGER, vwap REAL,
    PRIMARY KEY (code, date, bar_key)
);

-- DART 재무 지표 (기록용; 전략 조건 아님)
CREATE TABLE IF NOT EXISTS dart_fin (
    date TEXT, code TEXT,
    revenueGrowth REAL, earningsGrowth REAL, returnOnEquity REAL, debtToEquity REAL,
    qtr_rev_growth REAL, qtr_inc_growth REAL, qtr_label TEXT, annual_year INTEGER,
    rcept_dt TEXT, fiscal TEXT, rcept_dt_annual TEXT, rcept_dt_qtr TEXT,
    updated TEXT,
    PRIMARY KEY (date, code)
);

-- 야간 배치 실행 기록 (실패한 날은 감시 리스트를 쓰지 않는다 — 명세 11절)
CREATE TABLE IF NOT EXISTS runs (
    date TEXT, kind TEXT, status TEXT, detail TEXT, updated TEXT,
    PRIMARY KEY (date, kind)
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_path: str | None = None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 기존 DB 에 뒤늦게 추가된 컬럼 (SWING_TASK_AXIS_SCORE.md 1-1, 2-1). ALTER TABLE 은 멱등.
_MIGRATE_COLS: dict[str, list[tuple[str, str]]] = {
    "dart_fin": [("rcept_dt", "TEXT"), ("fiscal", "TEXT"),
                 ("rcept_dt_annual", "TEXT"), ("rcept_dt_qtr", "TEXT")],
    "signals": [("setup_score", "REAL"), ("setup_pscore", "REAL"),
                ("value_score", "REAL"), ("quality_score", "REAL"), ("growth_score", "REAL"),
                ("flow_score", "REAL"), ("liq_score", "REAL"), ("prog_score", "REAL"),
                ("total_score", "REAL"), ("rank_basis", "TEXT")],
    "watchlist": [("total_score", "REAL")],     # 감시 선정 기준(셋업+축 종합) — 대시보드 표시용
}


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in _MIGRATE_COLS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def init_db(path: str | None = None) -> None:
    """스키마 생성(멱등). path 를 주면 그 파일을 쓴다(테스트용)."""
    global _conn, _path
    p = path or cfg().db_path
    with _lock:
        if _conn is not None and _path == p:
            return
        if _conn is not None:
            _conn.close()
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(p, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(_SCHEMA)
        _migrate(c)
        c.commit()
        _conn, _path = c, p


def conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    return _conn  # type: ignore[return-value]


def close() -> None:
    global _conn, _path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _path = None, None


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = conn()
    with _lock:
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


# ── 일봉 ──────────────────────────────────────────────────────────────
def upsert_daily(rows: list[dict]) -> int:
    """rows: [{code,date,open,high,low,close,volume,value}]. 반환: 처리 건수."""
    if not rows:
        return 0
    now = _now()
    with tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO daily(code,date,open,high,low,close,volume,value,updated) "
            "VALUES (:code,:date,:open,:high,:low,:close,:volume,:value,:updated)",
            [{**r, "value": r.get("value"), "updated": now} for r in rows],
        )
    return len(rows)


def upsert_flow(rows: list[dict]) -> int:
    if not rows:
        return 0
    now = _now()
    with tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO flow(code,date,forgn,inst,indiv,updated) "
            "VALUES (:code,:date,:forgn,:inst,:indiv,:updated)",
            [{"indiv": None, **r, "updated": now} for r in rows],
        )
    return len(rows)


def upsert_fund(rows: list[dict]) -> int:
    if not rows:
        return 0
    now = _now()
    with tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO fund(code,date,per,pbr,eps,updated) "
            "VALUES (:code,:date,:per,:pbr,:eps,:updated)",
            [{**r, "updated": now} for r in rows],
        )
    return len(rows)


def upsert_program(rows: list[dict]) -> int:
    if not rows:
        return 0
    now = _now()
    with tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO program(code,date,ntby_qty,ntby_value,updated) "
            "VALUES (:code,:date,:ntby_qty,:ntby_value,:updated)",
            [{**r, "updated": now} for r in rows],
        )
    return len(rows)


def upsert_meta(rows: list[dict]) -> int:
    if not rows:
        return 0
    now = _now()
    with tx() as c:
        c.executemany(
            "INSERT INTO meta(code,name,market,mktcap,shares,updated) "
            "VALUES (:code,:name,:market,:mktcap,:shares,:updated) "
            "ON CONFLICT(code) DO UPDATE SET "
            "name=COALESCE(excluded.name,name), market=COALESCE(excluded.market,market), "
            "mktcap=COALESCE(excluded.mktcap,mktcap), shares=COALESCE(excluded.shares,shares), "
            "updated=excluded.updated",
            [{"name": None, "market": None, "mktcap": None, "shares": None, **r,
              "updated": now} for r in rows],
        )
    return len(rows)


def last_date(table: str, code: str) -> str | None:
    r = conn().execute(f"SELECT MAX(date) FROM {table} WHERE code=?", (code,)).fetchone()
    return r[0] if r and r[0] else None


def last_dates(table: str) -> dict[str, str]:
    """종목별 마지막 날짜 — 증분 수집 기준."""
    return {r[0]: r[1] for r in
            conn().execute(f"SELECT code, MAX(date) FROM {table} GROUP BY code")}


def count_rows(table: str, code: str) -> int:
    return int(conn().execute(f"SELECT COUNT(*) FROM {table} WHERE code=?",
                              (code,)).fetchone()[0])


def all_codes() -> list[str]:
    return [r[0] for r in conn().execute(
        "SELECT code FROM meta WHERE code NOT LIKE 'IDX%' ORDER BY code")]


def meta_map() -> dict[str, dict]:
    return {r["code"]: dict(r) for r in conn().execute("SELECT * FROM meta")}


def trading_dates(start: str, end: str, code: str | None = None) -> list[str]:
    """daily 에 있는 날짜(오름차순). code 없으면 전체 합집합."""
    if code:
        q = ("SELECT DISTINCT date FROM daily WHERE code=? AND date BETWEEN ? AND ? "
             "ORDER BY date")
        return [r[0] for r in conn().execute(q, (code, start, end))]
    q = "SELECT DISTINCT date FROM daily WHERE date BETWEEN ? AND ? ORDER BY date"
    return [r[0] for r in conn().execute(q, (start, end))]


def load_daily(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """종목별 일봉 DataFrame(DatetimeIndex, open/high/low/close/volume/value)."""
    if not codes:
        return {}
    out: dict[str, pd.DataFrame] = {}
    q = ("SELECT date,open,high,low,close,volume,value FROM daily "
         "WHERE code=? AND date BETWEEN ? AND ? ORDER BY date")
    for code in codes:
        df = pd.read_sql_query(q, conn(), params=(code, start, end))
        if df.empty:
            continue
        df.index = pd.to_datetime(df.pop("date"), format="%Y%m%d")
        df.index.name = "date"
        out[code] = df.astype(float)
    return out


def _load_table(table: str, cols: list[str], code: str, start: str, end: str) -> pd.DataFrame:
    q = (f"SELECT date,{','.join(cols)} FROM {table} "
         "WHERE code=? AND date BETWEEN ? AND ? ORDER BY date")
    df = pd.read_sql_query(q, conn(), params=(code, start, end))
    if df.empty:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], name="date"))
    df.index = pd.to_datetime(df.pop("date"), format="%Y%m%d")
    df.index.name = "date"
    return df.astype(float)


def load_panel_raw(codes: list[str], start: str, end: str,
                   meta: dict[str, dict] | None = None) -> dict[str, pd.DataFrame]:
    """bt_swing.indicators.compute 가 받는 형태로 합친다.

    컬럼: open/high/low/close/volume/value, forgn/inst/indiv, PER/PBR/EPS, mktcap/shares.
    bt_swing/data.py _load_one 과 같은 모양이라 지표·전략 코드를 그대로 쓴다.
    시총 이력은 없고(meta 의 최신값) 상수로 붙인다 — 게이트 용도.
    """
    px = load_daily(codes, start, end)
    meta = meta if meta is not None else meta_map()
    out: dict[str, pd.DataFrame] = {}
    for code, df in px.items():
        fl = _load_table("flow", ["forgn", "inst", "indiv"], code, start, end)
        fu = _load_table("fund", ["per", "pbr", "eps"], code, start, end)
        fu = fu.rename(columns={"per": "PER", "pbr": "PBR", "eps": "EPS"})
        d = df.join(fl, how="left").join(fu, how="left")
        for c in ("forgn", "inst", "indiv", "PER", "PBR", "EPS", "BPS", "DIV"):
            if c not in d.columns:
                d[c] = float("nan")
        m = meta.get(code) or {}
        d["mktcap"] = float(m["mktcap"]) if m.get("mktcap") else float("nan")
        d["shares"] = float(m["shares"]) if m.get("shares") else float("nan")
        out[code] = d.sort_index()
    return out


# ── 감시 리스트 / 신호 ─────────────────────────────────────────────────
_WL_COLS = ["date", "code", "strategy", "score", "rank_in_strategy", "rank_overall",
            "ref_ma20", "ref_ma60", "ref_prev_close", "ref_box_top", "ref_atr_pct",
            "ref_avg_bar_vol", "stop_px", "tp_px", "subscribed",
            "ref_value_ma20", "ref_mktcap", "pscore", "total_score"]


def save_watchlist(date: str, rows: list[dict]) -> None:
    """그 날짜의 감시 리스트를 통째로 교체한다."""
    with tx() as c:
        c.execute("DELETE FROM watchlist WHERE date=?", (date,))
        c.executemany(
            f"INSERT INTO watchlist({','.join(_WL_COLS)}) VALUES "
            f"({','.join(':' + k for k in _WL_COLS)})",
            [{k: None for k in _WL_COLS} | {**r, "date": date} for r in rows],
        )


def load_watchlist(date: str) -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM watchlist WHERE date=? ORDER BY rank_overall", (date,))]


def latest_watchlist_date() -> str | None:
    r = conn().execute("SELECT MAX(date) FROM watchlist").fetchone()
    return r[0] if r and r[0] else None


def set_subscribed(date: str, codes: list[str], flag: int) -> None:
    if not codes:
        return
    with tx() as c:
        c.executemany("UPDATE watchlist SET subscribed=? WHERE date=? AND code=?",
                      [(flag, date, cd) for cd in codes])


_SIG_COLS = ["date", "code", "strategy", "score", "rank_overall", "trigger_time",
             "trigger_px", "trigger_reason", "no_trigger_reason",
             "day_open", "day_high", "day_low", "day_close", "fwd1", "fwd3", "fwd5",
             "watched", "pscore", "trade_date",
             # 축별 점수 (기록용, SWING_TASK_AXIS_SCORE.md 2절). 비면 NULL.
             "setup_score", "setup_pscore", "value_score", "quality_score", "growth_score",
             "flow_score", "liq_score", "prog_score", "total_score", "rank_basis"]


def log_signal(row: dict) -> None:
    """(date, code, strategy) 기준 upsert. 주어진 키만 갱신한다(None 으로 덮지 않음)."""
    keys = [k for k in _SIG_COLS if k in row]
    if not all(k in row for k in ("date", "code", "strategy")):
        raise ValueError("log_signal: date/code/strategy 필수")
    upd = [k for k in keys if k not in ("date", "code", "strategy")]
    with tx() as c:
        c.execute(
            f"INSERT INTO signals({','.join(keys)}) VALUES ({','.join(':' + k for k in keys)}) "
            + ("ON CONFLICT(date,code,strategy) DO UPDATE SET "
               + ",".join(f"{k}=excluded.{k}" for k in upd) if upd else
               "ON CONFLICT(date,code,strategy) DO NOTHING"),
            {k: row[k] for k in keys},
        )


def log_signals(rows: list[dict]) -> None:
    for r in rows:
        log_signal(r)


def load_signals(date: str) -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM signals WHERE date=? ORDER BY rank_overall", (date,))]


# ── 분봉 ──────────────────────────────────────────────────────────────
def save_bar(bar, date: str | None = None) -> None:
    """kis_ws.Bar 저장(1분봉). date 없으면 오늘."""
    d = date or datetime.now().strftime("%Y%m%d")
    with tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO bars(code,date,bar_key,open,high,low,close,volume,ticks,vwap) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (bar.code, d, bar.key, bar.open, bar.high, bar.low, bar.close,
             bar.volume, bar.ticks, bar.vwap),
        )


def load_bars(code: str, date: str) -> pd.DataFrame:
    q = ("SELECT bar_key,open,high,low,close,volume,ticks,vwap FROM bars "
         "WHERE code=? AND date=? ORDER BY bar_key")
    return pd.read_sql_query(q, conn(), params=(code, date))


# ── 포지션 ────────────────────────────────────────────────────────────
_POS_COLS = ["mode", "code", "strategy", "state", "entry_date", "entry_time", "entry_px",
             "shares", "stop_px", "tp_px", "peak", "trail_on", "exit_date", "exit_time",
             "exit_px", "exit_reason", "order_no", "signal_date", "note",
             "fwd1", "fwd3", "fwd5"]


def open_positions(mode: str) -> list[dict]:
    """청산되지 않은 포지션(ARMED/ENTERED/HOLDING)."""
    return [dict(r) for r in conn().execute(
        "SELECT * FROM positions WHERE mode=? AND state IN ('ARMED','ENTERED','HOLDING') "
        "ORDER BY id", (mode,))]


def upsert_position(pos: dict) -> int:
    """id 가 있으면 갱신, 없으면 삽입. 반환: id."""
    p = {k: pos.get(k) for k in _POS_COLS}
    p["updated"] = _now()
    with tx() as c:
        if pos.get("id"):
            sets = ",".join(f"{k}=:{k}" for k in _POS_COLS + ["updated"])
            c.execute(f"UPDATE positions SET {sets} WHERE id=:id", {**p, "id": pos["id"]})
            return int(pos["id"])
        cols = _POS_COLS + ["updated"]
        cur = c.execute(
            f"INSERT INTO positions({','.join(cols)}) VALUES ({','.join(':' + k for k in cols)})",
            p,
        )
        return int(cur.lastrowid)


def load_positions(mode: str | None = None, state: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM positions", []
    where = []
    if mode:
        where.append("mode=?")
        args.append(mode)
    if state:
        where.append("state=?")
        args.append(state)
    if where:
        q += " WHERE " + " AND ".join(where)
    return [dict(r) for r in conn().execute(q + " ORDER BY id", args)]


# ── DART 재무 ─────────────────────────────────────────────────────────
_DART_COLS = ["date", "code", "revenueGrowth", "earningsGrowth", "returnOnEquity",
              "debtToEquity", "qtr_rev_growth", "qtr_inc_growth", "qtr_label", "annual_year",
              "rcept_dt", "fiscal", "rcept_dt_annual", "rcept_dt_qtr"]


def upsert_dart_fin(rows: list[dict]) -> int:
    if not rows:
        return 0
    q = (f"INSERT OR REPLACE INTO dart_fin({','.join(_DART_COLS)},updated) "
         f"VALUES ({','.join('?' * len(_DART_COLS))},?)")
    now = _now()
    with tx() as c:
        c.executemany(q, [tuple(r.get(k) for k in _DART_COLS) + (now,) for r in rows])
    return len(rows)


def load_dart_fin(date: str) -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM dart_fin WHERE date=?", (date,))]


def dart_fin_asof(date: str) -> dict[str, dict]:
    """종목별로 공시 접수일(rcept_dt) <= date 인 행 중 최신 1건. rcept_dt 없는 행은 제외
    (fiscal 기준으로 붙이면 미래참조라 쓰지 않는다)."""
    q = ("SELECT * FROM dart_fin WHERE rcept_dt IS NOT NULL AND rcept_dt<=? "
         "ORDER BY code, rcept_dt, date")
    out: dict[str, dict] = {}
    for r in conn().execute(q, (date,)):
        out[r["code"]] = dict(r)          # 정렬 오름차순이라 마지막 = 최신
    return out


def load_program_range(start: str, end: str) -> pd.DataFrame:
    """program(code,date,ntby_value) 구간 조회 (축 점수의 prog 재료용)."""
    return pd.read_sql_query(
        "SELECT code, date, ntby_value FROM program WHERE date BETWEEN ? AND ? ORDER BY code, date",
        conn(), params=(start, end))


# ── 실행 기록 ─────────────────────────────────────────────────────────
def mark_run(date: str, kind: str, status: str, detail: str = "") -> None:
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO runs(date,kind,status,detail,updated) VALUES (?,?,?,?,?)",
                  (date, kind, status, detail[:2000], _now()))


def get_run(date: str, kind: str) -> dict | None:
    r = conn().execute("SELECT * FROM runs WHERE date=? AND kind=?", (date, kind)).fetchone()
    return dict(r) if r else None
