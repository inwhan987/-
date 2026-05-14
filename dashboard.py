"""전략 파라미터 대시보드.

실행:
  python dashboard.py
  → http://localhost:8765 접속

기능:
  - .env.overrides 파라미터 조회/수정/저장
  - 백테스트 실행 (종목·기간 선택)
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env.overrides"

app = FastAPI(title="Stock Bot Dashboard")

# ── 파라미터 그룹 정의 ─────────────────────────────────────────────────────────
PARAM_GROUPS = [
    {
        "title": "앙상블 기본",
        "params": [
            {"key": "ENSEMBLE_WEIGHTS",        "label": "가중치 (vwap,st,rsi,bb,dc)", "type": "text"},
            {"key": "ENSEMBLE_BUY_THRESHOLD",  "label": "매수 임계값",   "type": "number", "step": "0.01"},
            {"key": "ENSEMBLE_SELL_THRESHOLD", "label": "매도 임계값",   "type": "number", "step": "0.01"},
            {"key": "ENSEMBLE_MIN_BUY_VOTES",  "label": "최소 매수 투표", "type": "number", "step": "1"},
            {"key": "ENSEMBLE_MIN_SELL_VOTES", "label": "최소 매도 투표", "type": "number", "step": "1"},
        ],
    },
    {
        "title": "VWAP",
        "params": [
            {"key": "TRADE_VWAP_BAND",               "label": "매수 이탈 기준",          "type": "number", "step": "0.0005"},
            {"key": "TRADE_VWAP_SELL_BAND",          "label": "매도 이탈 기준",          "type": "number", "step": "0.0005"},
            {"key": "TRADE_VWAP_ST_BULL_SELL_BAND",  "label": "ST 상승 시 매도 기준",    "type": "number", "step": "0.0005"},
            {"key": "TRADE_VWAP_WARMUP_BARS",        "label": "워밍업 봉수",             "type": "number", "step": "1"},
        ],
    },
    {
        "title": "RSI",
        "params": [
            {"key": "TRADE_RSI_PERIOD",      "label": "기간",      "type": "number", "step": "1"},
            {"key": "TRADE_RSI_OVERSOLD",    "label": "과매도 기준", "type": "number", "step": "1"},
            {"key": "TRADE_RSI_OVERBOUGHT",  "label": "과매수 기준", "type": "number", "step": "1"},
        ],
    },
    {
        "title": "Supertrend",
        "params": [
            {"key": "TRADE_SUPERTREND_PERIOD", "label": "기간",     "type": "number", "step": "1"},
            {"key": "TRADE_SUPERTREND_MULT",   "label": "배수 (k)", "type": "number", "step": "0.1"},
        ],
    },
    {
        "title": "추가매수",
        "params": [
            {"key": "ADD_BUY_ENABLED",          "label": "활성화",         "type": "text"},
            {"key": "ADD_BUY_THRESHOLD",        "label": "임계값",         "type": "number", "step": "0.01"},
            {"key": "ADD_BUY_MIN_VOTES",        "label": "최소 투표",      "type": "number", "step": "1"},
            {"key": "ADD_BUY_MAX_COUNT",        "label": "하루 최대 횟수", "type": "number", "step": "1"},
            {"key": "ADD_BUY_FRACTION",         "label": "계좌 비율",      "type": "number", "step": "0.01"},
            {"key": "ADD_BUY_MAX_POSITION_PCT", "label": "최대 포지션 비율","type": "number", "step": "0.01"},
        ],
    },
    {
        "title": "손절 (ATR)",
        "params": [
            {"key": "ATR_STOP_LOSS_ENABLED", "label": "활성화",       "type": "text"},
            {"key": "ATR_PERIOD",            "label": "ATR 기간",     "type": "number", "step": "1"},
            {"key": "ATR_STOP_MULTIPLIER",   "label": "ATR 배수",     "type": "number", "step": "0.5"},
            {"key": "ATR_STOP_MAX_PCT",      "label": "최대 손절 %",  "type": "number", "step": "0.1"},
        ],
    },
    {
        "title": "거래량 필터",
        "params": [
            {"key": "ENSEMBLE_VOLUME_FILTER_ENABLED", "label": "활성화",          "type": "text"},
            {"key": "ENSEMBLE_VOLUME_MA_PERIOD",      "label": "이동평균 기간",    "type": "number", "step": "1"},
            {"key": "ENSEMBLE_VOLUME_HIGH_RATIO",     "label": "고거래량 배수",    "type": "number", "step": "0.05"},
            {"key": "ENSEMBLE_VOLUME_LOW_RATIO",      "label": "저거래량 배수",    "type": "number", "step": "0.05"},
            {"key": "ENSEMBLE_VOLUME_SCORE_BOOST",    "label": "점수 가산",        "type": "number", "step": "0.01"},
            {"key": "ENSEMBLE_VOLUME_SCORE_PENALTY",  "label": "점수 감산",        "type": "number", "step": "0.01"},
        ],
    },
    {
        "title": "장초반 차단",
        "params": [
            {"key": "ENTRY_BLOCK_ENABLED",               "label": "활성화",             "type": "text"},
            {"key": "ENTRY_BLOCK_START",                 "label": "차단 시작",          "type": "text"},
            {"key": "ENTRY_BLOCK_END",                   "label": "차단 종료",          "type": "text"},
            {"key": "ENTRY_BLOCK_MIN_PROFIT_TO_SELL_PCT","label": "강제매도 수익 기준 %","type": "number", "step": "0.1"},
            {"key": "ENTRY_BLOCK_FORCE_SELL_FRACTION",   "label": "강제매도 비율",      "type": "number", "step": "0.1"},
        ],
    },
    {
        "title": "포지션 사이징",
        "params": [
            {"key": "POSITION_SIZING",   "label": "방식",      "type": "text"},
            {"key": "POSITION_FRACTION", "label": "계좌 비율", "type": "number", "step": "0.01"},
        ],
    },
]

ALL_KEYS = {p["key"] for g in PARAM_GROUPS for p in g["params"]}


def read_env() -> dict[str, str]:
    """현재 .env.overrides 읽기."""
    result: dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            v = v.split("#")[0].strip()  # 인라인 주석 제거
            result[k.strip()] = v
    return result


def write_env(updates: dict[str, str]) -> None:
    """변경된 값만 .env.overrides에 업데이트."""
    text = ENV_FILE.read_text(encoding="utf-8")
    for key, val in updates.items():
        # 기존 키=값 줄 교체
        pattern = rf"^({re.escape(key)}\s*=).*$"
        replacement = rf"\g<1>{val}"
        new_text, n = re.subn(pattern, replacement, text, flags=re.MULTILINE)
        if n == 0:
            # 없으면 파일 끝에 추가
            new_text = text.rstrip() + f"\n{key}={val}\n"
        text = new_text
    ENV_FILE.write_text(text, encoding="utf-8")


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/params")
def get_params():
    return JSONResponse(read_env())


class ParamUpdate(BaseModel):
    updates: dict[str, str]


@app.post("/api/params")
def save_params(body: ParamUpdate):
    # 허용된 키만 저장
    safe = {k: v for k, v in body.updates.items() if k in ALL_KEYS}
    write_env(safe)
    return {"ok": True, "saved": list(safe.keys())}


class BacktestRequest(BaseModel):
    symbol: str = "005930.KS"
    period: str = "60d"


@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    """백테스트 실행 후 결과 반환."""
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "backtest_current.py"), req.symbol, req.period],
            capture_output=True, text=True, timeout=300, cwd=str(ROOT),
        )
        output = result.stdout or result.stderr or "(출력 없음)"
        return {"ok": True, "output": output}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "타임아웃 (300초 초과)"}
    except Exception as e:
        return {"ok": False, "output": str(e)}


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Bot 대시보드</title>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3e;
    --accent: #4f8ef7; --green: #2ecc71; --red: #e74c3c;
    --text: #e0e0e0; --muted: #8892a4; --input-bg: #252837;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; font-size: 14px; }
  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 18px; font-weight: 600; }
  header .badge { background: var(--accent); color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 10px; }
  .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; }
  .card h2 { font-size: 13px; font-weight: 600; color: var(--accent); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
  .field { display: flex; align-items: center; margin-bottom: 10px; gap: 8px; }
  .field label { flex: 1; color: var(--muted); font-size: 13px; }
  .field input { width: 160px; background: var(--input-bg); border: 1px solid var(--border); border-radius: 6px; padding: 5px 10px; color: var(--text); font-size: 13px; transition: border-color .2s; }
  .field input:focus { outline: none; border-color: var(--accent); }
  .field input.changed { border-color: #f39c12; }
  .actions { margin: 20px 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  button { padding: 8px 20px; border: none; border-radius: 7px; font-size: 13px; font-weight: 600; cursor: pointer; transition: opacity .2s; }
  button:hover { opacity: .85; }
  #btn-save { background: var(--accent); color: #fff; }
  #btn-reset { background: var(--input-bg); color: var(--text); border: 1px solid var(--border); }
  .save-msg { font-size: 13px; color: var(--green); display: none; }
  .backtest-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-top: 16px; }
  .backtest-card h2 { font-size: 13px; font-weight: 600; color: var(--accent); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
  .bt-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
  .bt-row input, .bt-row select { background: var(--input-bg); border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px; color: var(--text); font-size: 13px; }
  #btn-bt { background: var(--green); color: #fff; }
  #bt-output { margin-top: 14px; background: #0a0c14; border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-family: 'Consolas', monospace; font-size: 12px; white-space: pre; overflow-x: auto; min-height: 60px; color: #b0ffb0; display: none; }
  #bt-spinner { display: none; color: var(--muted); font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>📈 Stock Bot 대시보드</h1>
  <span class="badge">파라미터 관리</span>
</header>
<div class="container">
  <div class="actions">
    <button id="btn-save">💾 저장</button>
    <button id="btn-reset">↩ 초기화</button>
    <span class="save-msg" id="save-msg">✓ 저장됐습니다</span>
  </div>
  <div class="grid" id="param-grid"></div>

  <!-- 백테스트 섹션 -->
  <div class="backtest-card">
    <h2>🔬 백테스트</h2>
    <div class="bt-row">
      <input id="bt-symbol" value="005930.KS" placeholder="종목 (예: 005930.KS)">
      <select id="bt-period">
        <option value="30d">30일</option>
        <option value="60d" selected>60일</option>
        <option value="90d">90일</option>
      </select>
      <button id="btn-bt">▶ 실행</button>
      <span id="bt-spinner">⏳ 실행 중...</span>
    </div>
    <pre id="bt-output"></pre>
  </div>
</div>

<script>
const GROUPS = %GROUPS%;
let original = {};

async function loadParams() {
  const res = await fetch('/api/params');
  original = await res.json();
  renderGroups();
}

function renderGroups() {
  const grid = document.getElementById('param-grid');
  grid.innerHTML = '';
  GROUPS.forEach(g => {
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `<h2>${g.title}</h2>`;
    g.params.forEach(p => {
      const val = original[p.key] ?? '';
      const field = document.createElement('div');
      field.className = 'field';
      field.innerHTML = `
        <label title="${p.key}">${p.label}</label>
        <input id="${p.key}" data-key="${p.key}" type="${p.type === 'number' ? 'number' : 'text'}"
          ${p.step ? `step="${p.step}"` : ''} value="${val}">
      `;
      card.appendChild(field);
    });
    grid.appendChild(card);
  });

  // 변경 감지
  document.querySelectorAll('.field input').forEach(inp => {
    inp.addEventListener('input', () => {
      inp.classList.toggle('changed', inp.value !== (original[inp.dataset.key] ?? ''));
    });
  });
}

document.getElementById('btn-save').addEventListener('click', async () => {
  const updates = {};
  document.querySelectorAll('.field input').forEach(inp => {
    if (inp.value !== (original[inp.dataset.key] ?? '')) {
      updates[inp.dataset.key] = inp.value;
    }
  });
  if (!Object.keys(updates).length) { alert('변경된 값이 없습니다.'); return; }
  const res = await fetch('/api/params', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({updates}),
  });
  const data = await res.json();
  if (data.ok) {
    Object.assign(original, updates);
    document.querySelectorAll('.field input.changed').forEach(i => i.classList.remove('changed'));
    const msg = document.getElementById('save-msg');
    msg.style.display = 'inline';
    setTimeout(() => msg.style.display = 'none', 2500);
  }
});

document.getElementById('btn-reset').addEventListener('click', () => {
  document.querySelectorAll('.field input').forEach(inp => {
    inp.value = original[inp.dataset.key] ?? '';
    inp.classList.remove('changed');
  });
});

document.getElementById('btn-bt').addEventListener('click', async () => {
  const symbol = document.getElementById('bt-symbol').value.trim();
  const period = document.getElementById('bt-period').value;
  const out = document.getElementById('bt-output');
  const spinner = document.getElementById('bt-spinner');
  out.style.display = 'none';
  spinner.style.display = 'inline';
  document.getElementById('btn-bt').disabled = true;
  try {
    const res = await fetch('/api/backtest', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol, period}),
    });
    const data = await res.json();
    out.textContent = data.output;
    out.style.display = 'block';
  } finally {
    spinner.style.display = 'none';
    document.getElementById('btn-bt').disabled = false;
  }
});

loadParams();
</script>
</body>
</html>
"""


def _groups_json() -> str:
    import json
    return json.dumps(PARAM_GROUPS, ensure_ascii=False)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(HTML.replace("%GROUPS%", _groups_json()))


if __name__ == "__main__":
    print("대시보드 시작: http://localhost:8765")
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
