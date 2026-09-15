# 일봉 스윙봇 설계 · 구현 명세

기존 3분봉 대장주봇(`leader_finder.py`)과 **별도로 도는** 스윙 전략 봇.
일봉으로 종목을 고르고, 분봉으로 진입 타점을 잡고, 며칠 보유한다.

**모든 모듈을 한 번에 구현한다.** 드라이런/모의/실전은 단계가 아니라
설정값(`SWING_MODE`)으로 갈린다 — 코드는 전부 존재하고 주문 단계만 분기한다.

---

## 0. 배경 — 왜 이 봇을 만드는가

백테스트(`bt_swing/`)를 2022-01~2025-08 전종목(2,483)으로 돌린 결과,
**11개 전략 모두 통계적 엣지가 없었다.**

```
선택력(초과수익) t값 최대        4.16  (FLOW_FORGN, 겹침 보정 시 ~1)
청산 파라미터 36조합 중 플러스   396개 중 3개
결합(합집합/교집합/앙상블)       전부 개별 최고보다 나쁨
```

다만 백테스트가 **끝까지 검증하지 못한 것이 하나** 남아 있다 —
**분봉 진입 확인**. 백테스트는 신호 다음날 시가에 무조건 샀고, NEWHIGH의
손절(갭)이 130건 평균 −11.54%였다. 비싸게 사서 털린 것이다.

이 봇은 그 미검증 부분을 실측한다. **수익이 1차 목적이 아니다.**

### 확인된 사실

- `leader_finder.py`는 **WebSocket을 쓰지 않는다** (참조 0건). REST 폴링만 쓴다.
  → WS 41종목 한도를 스윙봇이 온전히 다 쓸 수 있다.
- `kis_ws.py`의 `stream_ticks`는 `main.py stream` CLI에서만 쓰인다.
  → 재작성하되 그 시그니처만 유지하면 된다.

### 절대 하지 말 것

- `leader_finder.py`, `screener.py` 수정 금지. 스윙봇은 새 파일로.
- 전략 조건을 새로 짜지 말 것. `bt_swing/strategies.py`를 **import 재사용**.
  백테스트와 실전이 같은 코드로 신호를 내야 둘을 비교할 수 있다.
- 백테스트 성과가 좋아지도록 파라미터를 맞추지 말 것.

---

## 1. 모드

```ini
SWING_MODE=dryrun   # 주문 없음. dryrun 테이블에 기록만
SWING_MODE=paper    # 모의투자 주문
SWING_MODE=live     # 실전 주문
```

**세 모드가 같은 파이프라인을 탄다.** 신호 생성·감시·트리거 판정까지 완전히
동일하고, `orders.place()`에서만 갈린다.

```python
# orders.py
def place(mode, code, side, qty, px) -> dict:
    if mode == "dryrun":
        return {"filled": True, "px": px, "simulated": True}   # 기록만
    broker = KISBroker()          # paper/live 는 계정 설정이 다를 뿐
    return broker.place_order(...)
```

`dryrun` 에서도 `positions` 테이블에 가상 체결을 기록하고 청산까지 돌린다.
그래야 "주문했다면 어떻게 됐을지"가 그대로 남는다.

---

## 2. 전체 흐름

```
[야간 배치] 18:00
  KIS REST → 전종목 일봉 수집 → SQLite
           → 지표 계산 → 전략 타점 판정 → 점수
           → 감시 리스트 생성 → 기준선 계산
                    ↓
[장 시작 전] 08:45
  감시 리스트 로드 → (필요시) 당일 분봉 워밍업
                    ↓
[장중] 09:00~15:30
  WebSocket 구독 (REST 호출 0)
    틱 → 1분봉(저장) + 3분봉(판정) 합성
    3분봉 확정 → 트리거 판정 → orders.place(mode)
    틱 → 보유 종목 손절 감시
                    ↓
[장 마감] 15:20
  미확정 봉 flush → 청산 판정 → 기록
                    ↓
[사후] 익일 이후
  dryrun/positions 의 fwd1/fwd3/fwd5 채우기
```

---

## 3. 데이터 계층

### 3-1. SQLite — `data/swing.db`

```sql
CREATE TABLE daily (
    code TEXT NOT NULL, date TEXT NOT NULL,      -- YYYYMMDD
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER, value INTEGER, updated TEXT,
    PRIMARY KEY (code, date)
);
CREATE INDEX idx_daily_date ON daily(date);

CREATE TABLE meta (
    code TEXT PRIMARY KEY, name TEXT, market TEXT,
    mktcap INTEGER, shares INTEGER, updated TEXT
);

CREATE TABLE watchlist (
    date TEXT, code TEXT, strategy TEXT,
    score REAL, rank_in_strategy INTEGER, rank_overall INTEGER,
    ref_ma20 REAL, ref_ma60 REAL, ref_prev_close REAL,
    ref_box_top REAL, ref_atr_pct REAL, ref_avg_bar_vol REAL,
    stop_px REAL, tp_px REAL,
    subscribed INTEGER,
    PRIMARY KEY (date, code, strategy)
);

-- 감시했으나 진입하지 않은 것까지 전부 기록 (모드 무관)
CREATE TABLE signals (
    date TEXT, code TEXT, strategy TEXT,
    score REAL, rank_overall INTEGER,
    trigger_time TEXT,           -- HHMMSS. 미발생이면 NULL
    trigger_px REAL,
    trigger_reason TEXT,
    no_trigger_reason TEXT,      -- 미발생 사유
    day_open REAL, day_high REAL, day_low REAL, day_close REAL,
    fwd1 REAL, fwd3 REAL, fwd5 REAL,    -- 사후 채움
    PRIMARY KEY (date, code, strategy)
);

CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT,                   -- dryrun/paper/live
    code TEXT, strategy TEXT, state TEXT,
    entry_date TEXT, entry_time TEXT, entry_px REAL, shares INTEGER,
    stop_px REAL, tp_px REAL, peak REAL, trail_on INTEGER,
    exit_date TEXT, exit_time TEXT, exit_px REAL, exit_reason TEXT
);

CREATE TABLE bars (
    code TEXT, date TEXT, bar_key TEXT,          -- HHMM
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER, ticks INTEGER, vwap REAL,
    PRIMARY KEY (code, date, bar_key)
);
```

**`positions`는 반드시 DB에 있어야 한다.** 프로세스가 죽어도 포지션을 잃지
않아야 재시작 시 손절을 할 수 있다. 메모리에만 두면 안 된다.

**`signals`는 모드와 무관하게 항상 쓴다.** 진입 안 한 것까지 남아야
"트리거가 좋은 걸 놓쳤나"를 사후에 볼 수 있다.

### 3-2. 수집

**야간 백필에서 전부 받는다.** 장중에는 WebSocket 외에 아무것도 안 받는다.

| 데이터 | 쓰는 전략 | 소스 | 주기 | 호출 |
|---|---|---|---|---|
| 전종목 일봉 (초기 **400봉**) | 전부 | KIS REST | 1회성 | 2,500 × 4 = 10,000 |
| 전종목 일봉 (증분) | 전부 | KIS REST | 매일 18:00 | 2,500 |
| **투자자별 수급** (외국인·기관) | FLOW_* 4종 | KIS REST | 매일 18:00 | 2,500 |
| **PER / PBR** | VALUE_* 2종 | KIS REST | 매일 18:00 | 2,500 |
| **프로그램매매** | (축 추가 검토) | KIS REST | 매일 18:00 | 2,500 |
| 시총·상장주식수 | 게이트 | KIS REST | 주 1회 | 2,500 |
| 장중 시세 | 전부 | **WebSocket** | 실시간 | **0** |

초기 적재가 300봉이 아니라 **400봉**인 이유: `hh250`(52주 신고가)에 250봉,
`bb_width_pct`는 `rolling(120)` 위에 rank 라 120+ 가 더 필요하다. 300이면
워밍업 여유가 없어 초반 지표가 NaN 이 된다.

소요: 모의(초당 1건) 초기 3시간 / 매일 2.8시간. 실전(초당 20건) 8분 / 8분.
**매일 2.8시간은 모의로는 부담이다. 일봉·수급 수집은 실전 계정으로 하고
매매만 모의로** 하는 분리를 권한다 (계정이 달라도 데이터는 같다).

### 3-3. ⚠️ 수급·프로그램매매·PER/PBR 은 KIS 가용성 확인이 먼저다

**KIS REST 에 '과거 일별' 수급·프로그램매매가 있는지 공개 자료로 확정이
안 된다.** 검색으로 확인된 것은 아래 둘인데 둘 다 당일 위주로 보인다.

```
[국내주식-114] comp-program-trade-today    프로그램매매 종합현황(시간)
[국내주식-037] foreign-institution-total   외국인 매매종목 가집계
```

`test_kis_data.py` 를 먼저 돌려 무엇이 되는지 확인한다. 이 스크립트는 8개
후보 엔드포인트를 찔러보고, 응답 필드에 외국인·기관·프로그램·PER·PBR 이
있는지와 **응답에 포함된 날짜가 몇 개인지**를 출력한다.

| 결과 | 대응 |
|---|---|
| 날짜가 여러 개 | 과거 일별 조회 가능. 초기 적재 한 번으로 끝 |
| 날짜가 1개 | **당일치만.** 매일 저녁 1회씩 쌓아야 하고, 과거는 다른 소스 필요 |
| 전부 실패 | `pykrx` 로 받는다. 단 **차단 이력이 있으니 `rps 0.7`**, 종목당 1회로 기간 전체가 오므로 2,500회 |

**과거분이 KIS 로 안 되면** `bt_swing/data.py` 의 pykrx 수집기를 그대로
재사용해 초기 적재만 하고, 이후 증분은 KIS 로 매일 쌓는 하이브리드가 된다.

### 3-4. 수급은 '전일 확정치'면 되지만 '하루치'로는 안 된다

장중 실시간 수급은 **필요 없다.** 전일 확정치로 충분하다.
다만 지표를 만들려면 **이력**이 필요하다.

| 지표 | 필요 이력 |
|---|---|
| `forgn_streak >= 3` | 3일 |
| `forgn_streak.shift(1) < 3` | 4일 |
| `forgn_intensity` (5일 누적 ÷ 20일 평균 거래대금) | **20일** |
| `forgn_sum20` (FLOW_PULLBACK) | **20일** |

오늘부터 쌓기 시작하면 **FLOW 전략 4종이 20영업일(약 1개월) 뒤에야
정상 작동한다.** 그 전까지는 신호가 안 나거나 왜곡된다.

→ **초기 적재로 과거 60영업일치를 한 번 채운다.** pykrx 는 종목당 1회
호출로 기간 전체를 주므로 2,500회, `rps 0.7` 로 약 1시간이면 끝난다.
이후 증분은 매일 KIS 로 쌓는다.

프로그램매매는 아직 축도 전략도 없으므로 **오늘부터 쌓아도 된다.**
3개월쯤 모이면 IC 를 재보고 전략을 붙일지 정한다.

> 대안: `FinanceData/marcap`을 `git pull`하면 1995~현재 전종목 일별
> OHLCV·거래대금·시총·상장주식수가 API 호출 0으로 온다. 단 **비수정주가**라
> `Stocks`(상장주식수) 변화로 분할 보정이 필요하다.

---

## 3-5. DART 재무 축 (퀄리티·성장)

지금까지 쓴 PER/PBR 은 KRX 공표치로 **밸류**다. **퀄리티·성장**은 한 번도
측정하지 않았다. 원래 설계했던 "재무 등급 A~D"가 이 축이다.

밸류가 IC t −22.0 / −18.1 로 전 팩터 중 가장 강했으므로, 재무 쪽을 더
볼 근거는 있다. 다만 **가점으로 쓸 값어치가 있는지 먼저 측정한다.**

### 뽑을 항목

| 지표 | 계산 | 성격 |
|---|---|---|
| ROE | 당기순이익 ÷ 자기자본 | 퀄리티 |
| 영업이익률 | 영업이익 ÷ 매출액 | 퀄리티 |
| 부채비율 | 부채총계 ÷ 자기자본 | 안정성 |
| 매출 YoY | 전년 동기 대비 | 성장 |
| 영업이익 YoY | 전년 동기 대비 | 성장 |

### ⚠️ 시점 정합이 핵심이다 — 여기서 틀리면 전부 무의미

**재무제표는 분기 말이 아니라 '공시 접수일' 이후에만 알 수 있다.**

```
2025년 3분기 재무 (기준일 2025-09-30)
  → 실제 공시는 11월 중순

9/30 기준으로 붙이면 = 약 6주 미래참조
```

반드시 이렇게 저장한다.

```sql
CREATE TABLE dart_fin (
    code TEXT, fiscal TEXT,       -- 2025Q3 같은 회계 분기
    rcept_dt TEXT,                -- 공시 접수일 ← 이게 '알게 된 날'
    roe REAL, opm REAL, debt_ratio REAL,
    sales_yoy REAL, op_yoy REAL,
    PRIMARY KEY (code, fiscal)
);
```

조회 시에는 **`rcept_dt <= 기준일`인 것 중 가장 최근 것**을 쓴다.

```python
def fin_as_of(code: str, date: str) -> dict | None:
    """date 시점에 '알 수 있었던' 재무. rcept_dt 기준."""
```

`fiscal` 기준으로 붙이면 미래참조다. `rcept_dt` 없이 저장하면 나중에
고칠 수도 없으니 **수집 단계에서 반드시 같이 받는다.**

### 수집

- OpenDART. `opendartreader`가 이미 `requirements.txt`에 있다.
- **하루 2만건 제한.** KRX 같은 IP 차단은 없다.
- 고유번호(corp_code) ↔ 종목코드 매핑을 먼저 만든다 (zip 파일 1회 다운로드).
- 전종목 5년치 = 약 1만 요청 → 하루면 끝난다.
- 연결재무제표 우선, 없으면 별도재무제표.

### 측정 — 봇에 얹기 전에 먼저 잰다

`bt_swing/report.py`의 `selection_report()`에 `fund_cols`로 넣으면
분위별 초과수익이 바로 나온다.

```python
fund_cols = ("per", "pbr", "value_eok", "mktcap_eok",
             "roe", "opm", "debt_ratio", "sales_yoy", "op_yoy")
```

```
[roe]  1분위 −0.8%   2분위 −0.2%   3분위 +0.3%   4분위 +0.9%   5분위 +1.6%
```

**채택 기준**

```
분위별 초과수익이 단조롭고(1→5 또는 5→1)
IC t ≥ 3 이며 5/10/20일 방향이 일치하면 → 가점으로 채택
들쭉날쭉하면 → 배점 0. 노이즈만 섞인다
```

### 채택되면 — 등급으로 쓴다

```
퀄리티 점수 = 채택된 축들의 백분위 평균 (동일가중)
  A: 상위 20%   B: 20~50%   C: 50~80%   D: 하위 20%
```

등급은 **컷이 아니라 사이징·트리거 강도**에 쓴다.

| 등급 | 사이즈 | 트리거 |
|---|---|---|
| A | 100% | 첫 트리거에 진입 |
| B | 80% | 첫 트리거에 진입 |
| C | 40% | 확인 조건 추가 (거래량·시가대비 플러스 등) |
| D | 진입 없음 | 감시만 |

**배점을 IC 크기에 비례시키지 말 것.** 표본 노이즈까지 따라간다. 채택된
축은 동일가중으로 평균낸다.

---

## 4. 전략 — 조건의 성격을 구분한다

지금 `bt_swing/strategies.py`는 성격이 다른 조건을 전부 AND로 묶어놨다.
그래서 **11개 전략이 전부 "상승추세일 것"을 공유**했고, 그 전제가 이 기간
역신호(t −12~−14)라 11개가 통째로 죽었다.

| 성격 | 뜻 | 처리 |
|---|---|---|
| **정의** | 없으면 그 전략이 아닌 것 | AND 필수 |
| **게이트** | 살 수 있느냐 / 과열 배제 | AND 필수 |
| **가점** | 있으면 좋지만 없어도 되는 것 | 점수로 (※ 4-3 참고) |
| **전제** | 시장에 대한 가정 | 제거 또는 별도 측정 |

**IC 근거** (전종목 유효 실행, 20일 선행수익률)

```
양(+)  vol_ratio +10.8   near_52w +6.9   vol_dry +3.0   forgn_streak +2.4
음(−)  pbr −22.0   atr_pct_rank −18.4   per −18.1   ret60 −14.4
       ma60_slope −12.5   ret20 −11.9   ret120 −10.6   disp20 −9.6
```

음(−)은 "값이 클수록 나쁘다". `pbr −22.0`은 **저PBR이 유리**,
`ma60_slope −12.5`는 **추세 기울기가 역신호**라는 뜻이다.

### 4-1. 전략별 재분류

#### BREAKOUT — 변동성 수축 후 돌파
| 조건 | 성격 | 조치 |
|---|---|---|
| `close > hh20` | 정의 | AND |
| `disp20 < 0.25` | 게이트 | AND (0.15로 좁혀도 됨) |
| `vol_ratio > 1.5` | 가점 | 점수 — t+10.8, 최강 팩터인데 1.5 컷은 임의 |
| `bb_width_pct.shift(1) < 0.40` | 가점 | 점수 |
| `close > open` | 가점 | 점수 |
| `ma20 > ma60` | **전제** | **제거** |

#### PULLBACK — 20일선 눌림 후 반등
| 조건 | 성격 | 조치 |
|---|---|---|
| 3일 저가 ≤ `ma20`×1.02 | 정의 | AND |
| `close > ma20` | 정의(회복) | AND |
| `close > open` | 정의 보조 | AND |
| `disp20` −2~+8% | 게이트 | AND |
| `ret20 > −0.15` | 게이트 | 재검토 (`ret20` 자체가 역신호) |
| `aligned == 1` | **전제** | **제거** |
| `ma60_slope > 0` | **전제** | **제거** |

#### NEWHIGH — 52주 신고가
| 조건 | 성격 | 조치 |
|---|---|---|
| `close >= hh250` | 정의 | AND — `near_52w` t+6.9, **최강 양의 팩터** |
| `vol_ratio > 1.2` | 가점 | 점수 |
| `close > open` | 가점 | 점수 |
| `ma20 > ma60` | **전제** | **제거** |

11개 중 구조가 가장 건강하다. 셋업의 정의 자체가 가장 강한 팩터다.

#### MEANREV — RSI(2) 과매도
| 조건 | 성격 | 조치 |
|---|---|---|
| `rsi2 < 10` | 정의 | AND |
| `close < ma5` | 정의 | AND |
| `ret5 > −0.20` | 게이트 | AND |
| `above_ma200` | **전제** | **제거** |

#### FLOW_FORGN — 외국인 연속 순매수
| 조건 | 성격 | 조치 |
|---|---|---|
| `forgn_streak >= 3` | 정의 | AND (t+2.4) |
| `shift(1) < 3` (첫날만) | 정의 | AND |
| `forgn_intensity > 0.03` | 가점 | 점수 |
| `ma20 > ma60`, `close > ma20` | **전제** | **제거** — 이것 뺀 게 t를 −0.63→**+4.16**으로 뒤집음 |

#### FLOW_INST — 기관 연속 순매수
FLOW_FORGN과 같은 구조. **방향이 엇갈린다** — 200종목 실행에서
`inst_streak` t−3.3, `inst_intensity` t−7.2로 강한 역신호였는데,
전종목 no-trend에서는 t+3.96. 관찰 대상.

#### GAPGO — 갭 상승 이어달리기
조건 유지, `ma20 > ma60`만 제거. **초과수익 −1.29%, t−3.53으로
11개 중 유일하게 통계적으로 나쁨.**

#### MOMENTUM — 주간 모멘텀
```
weekly + above_ma200 + ret20>0 + ret60>0 + ma20>ma60
         ↑ 이 넷이 전부 전제이고 전부 역신호 팩터
```
전제를 빼면 **조건이 "월요일"밖에 안 남는다.** no-trend 초과수익 +0.03%
(신호 34만 건) = 사실상 무작위.

#### FLOW_BOTH (t+0.05) / FLOW_PULLBACK (t−2.17)
초과수익이 0 근처거나 음수.

#### VALUE_MOM → **VALUE_PURE 변형 추가**
| 조건 | 성격 | 조치 |
|---|---|---|
| `eps_pos`, `per 1~15`, `pbr 0.2~2.0` | 정의 | AND — **`pbr` t−22.0, 전 팩터 중 최강** |
| `vol_ratio > 1.3` | 가점 | 점수 |
| `above_ma200` | **전제** | **제거** |
| `close > hh20` | ??? | **제거해서 VALUE_PURE로 분리** |

저PBR 가치주를 사려는 전략인데 "20일 신고가 돌파"를 AND로 걸어놔서
**소외된 가치주를 사는 게 목적인데 이미 오른 것만 사게** 만들었다.
밸류와 모멘텀을 AND로 묶어 둘 다 죽인 것이다. 신호 9,345건뿐인 게 증거.

→ `VALUE_MOM`(돌파 포함)과 `VALUE_PURE`(돌파 제거) **둘 다** 등록해서 비교.

### 4-2. 전략 선택은 결과를 보고 정한다

`SWING_STRATEGIES` 설정으로 켜고 끈다. **12개 전부 구현해 놓고,
어느 것을 돌릴지는 실측 결과를 보고 정한다.**

```ini
SWING_STRATEGIES=ALL
# 또는
SWING_STRATEGIES=FLOW_FORGN,NEWHIGH,VALUE_PURE
```

### 4-3. 가점 이동은 나중에

조건을 점수로 옮기는 것(`vol_ratio > 1.5` → 배점)은 **점수 변별력이
확인된 뒤에** 한다. 지금 점수는 Q5−Q1 ≈ 0으로 변별력이 없어서, 조건을
점수로 옮기면 그냥 아무거나 사는 것이 될 수 있다.

우선 `bt_swing`에서 `--daily-top 20/41/100` 을 측정해 점수가 상위 N개를
골라내는지 확인한다. **그 전까지는 조건을 그대로 둔다.**

추세 필터는 `set_trend_filter(False)`로 끈 채 돌린다.

---

## 5. 모듈 구성

```
stock_bot/swing/
  __init__.py
  store.py          SQLite 스키마·적재·조회
  collector.py      KIS REST 일봉 수집 (증분·재시도·유량제어)
  dart.py           OpenDART 재무 수집 (rcept_dt 기준 시점정합 필수)
  daily_scan.py     지표+전략 판정+점수 → watchlist
  levels.py         감시 종목 기준선 계산
  regime.py         시장 레짐 판정 (지수 200일선, ON/OFF)
  triggers.py       분봉 트리거 (전략 유형별)
  orders.py         모드별 주문 분기 (dryrun/paper/live)
  exits.py          청산 판정
  state.py          상태 머신 + positions 관리
  live.py           장중 러너

stock_bot/broker/
  kis_ws.py         ★ 재작성 — 재연결·PINGPONG·ack검증·BarBuilder
                       기존 stream_ticks 시그니처는 유지 (main.py stream 호환)

scripts/
  swing_nightly.py  야간 배치
  swing_live.py     장중 러너
  swing_fill_fwd.py signals/positions 의 fwd1/3/5 사후 채우기
```

### 5-1. `kis_ws.py` 재작성 (A안)

현재 `kis_ws.py`에는 6시간 30분을 못 버티는 문제가 있다.

| 문제 | 결과 |
|---|---|
| PINGPONG을 `continue`로 무시 | **서버가 연결을 끊는다.** 반드시 `ws.pong(raw)` 응답 |
| 등록 ack를 로그만 찍음 | 한도 초과를 모르고 "감시 중"이라고 착각 |
| 재연결 없음 | 끊기면 그날 끝 |
| ack 대기가 틱과 섞임 | 장중 등록 시 순서 어긋남 |

`stock_bot/broker/kis_ws_swing.py`에 고친 구현이 이미 있다.
**그 내용을 `kis_ws.py`로 옮기고**, 기존 함수는 얇은 래퍼로 남긴다.

```python
# 유지해야 할 시그니처 (main.py stream 이 씀)
async def stream_ticks(symbols: Iterable[str]) -> AsyncIterator[Tick]:
    """새 SwingTickStream 을 감싸서 틱만 yield."""
```

옮긴 뒤 `kis_ws_swing.py`는 삭제한다.

### 5-2. 함수 시그니처

```python
# store.py
def init_db(path: str = "data/swing.db") -> None
def upsert_daily(rows: list[dict]) -> int
def load_daily(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]
def save_watchlist(date: str, rows: list[dict]) -> None
def load_watchlist(date: str) -> list[dict]
def log_signal(row: dict) -> None
def save_bar(bar) -> None
def open_positions(mode: str) -> list[dict]
def upsert_position(pos: dict) -> int

# collector.py
def collect_daily(codes, count=300, rps=1.0, budget_sec=0) -> dict
    """실패는 캐시에 남기지 않고 다음 회차 재시도.
       연속 실패 80% 넘으면 Blocked 예외로 중단."""

# daily_scan.py
def scan(date, strategies=None, use_trend=False) -> list[dict]
    """bt_swing.strategies 재사용. 반환: [{code, strategy, score, ...}]"""
def build_watchlist(signals, mode: str, n: int) -> list[dict]
    """mode='even'  전략별 상위 n//len(strategies) 개씩
       mode='top'   전체 점수 상위 n 개"""

# levels.py
def compute_levels(code, daily: pd.DataFrame) -> dict
    """ref_ma20/ma60/prev_close/box_top/atr_pct/avg_bar_vol, stop_px, tp_px"""

# triggers.py
def check(strategy: str, bar, lv: dict, session: dict) -> tuple[bool, str]
    """(발생여부, 사유). session 은 당일 누적(시가·저가·누적거래량)"""

# orders.py
def place(mode, code, side, qty, px) -> dict
def cancel(mode, order) -> bool

# exits.py
def check_tick(pos, price, cfg) -> tuple[bool, str, float] | None
def check_bar(pos, bar, cfg) -> tuple[bool, str, float] | None
def check_eod(pos, day_close, cfg) -> tuple[bool, str, float] | None
```

---

## 6. 감시 리스트

WebSocket 한도는 **세션당 41종목**(체결+호가 합산)으로 알려져 있으나
자료마다 20/41/60으로 엇갈린다. `test_ws_limit.py`로 실측한다.
**호가(H0STASP0)는 구독하지 않는다** — 체결만 쓰면 한도를 전부 종목 수에
쓸 수 있다. 대장주봇이 WS를 안 쓰므로 한도를 스윙봇이 온전히 다 쓴다.

### 6-1. 슬롯 배분 — 보유 종목도 구독해야 한다

보유 중인 종목은 **손절을 틱으로 감시해야** 하므로 WS 슬롯을 먹는다.
한도에 여유를 두고 이렇게 나눈다.

```
신규 감시   30종목
보유 감시    6종목  (최대)
────────────────
합계        36종목   ← 한도 41 에 5칸 여유
```

전략 수가 많을수록 전략당 몫이 줄어든다. `even` 모드 기준:

```
12개 전략 → 전략당 2~3개
 6개 전략 → 전략당 5개
```

전략을 좁힐수록 각 전략을 깊게 관찰할 수 있다. 설정값이라 바꾸기 쉬우니
실측 결과를 보고 정한다.

보유가 늘면 신규 감시를 그만큼 줄인다.

```python
n_new = SWING_WATCH_NEW - max(0, len(holdings) - SWING_WATCH_HOLD)
```

여유 5칸은 재연결 도중 이전 구독이 아직 안 풀린 경우를 위한 완충이다.

### 6-2. 배분 방식

| 배분 | 용도 |
|---|---|
| `even` — 전략별 균등 | 전략을 여러 개 관찰할 때. 점수 상위로 자르면 신호 많은 전략이 독식해서 GAPGO 같은 소수 전략의 타점 발생률을 못 본다 |
| `top` — 전체 점수 상위 | 전략을 2~3개로 좁힌 뒤 |

### 6-3. 같은 종목을 여러 전략이 신호 낼 때

**종목 단위로 최고 점수 전략 하나만 채택한다.** WS 구독은 종목 단위라
슬롯을 중복으로 쓸 이유가 없고, 어느 전략이 더 좋은 타점을 봤는지는
점수가 답한다.

```python
# 전략마다 점수 분포가 다르므로 그대로 비교하면 안 된다.
# 전략 내부 백분위로 정규화한 뒤 종목별 최고를 고른다.
df["pscore"] = df.groupby("strategy")["score"].rank(pct=True) * 100
best = df.sort_values("pscore", ascending=False).drop_duplicates("code")
```

채택되지 않은 전략도 `signals` 테이블에는 **전부 기록한다.** 나중에
"어느 전략이 같은 종목을 몇 번 같이 짚었나", "겹친 종목이 더 좋았나"를
볼 수 있어야 한다.

---

## 7. 트리거 — 전략 유형별로 달라야 한다

기준선은 야간에 미리 계산해 `watchlist`에 박아둔다. 장중에는 비교만 한다.

```python
# 돌파형 (BREAKOUT / NEWHIGH / GAPGO / VALUE_MOM)
def breakout_trigger(bar, lv, s):
    return (bar.close > lv["ref_box_top"]
            and bar.volume > lv["ref_avg_bar_vol"] * 1.5)

# 눌림목형 (PULLBACK / FLOW_PULLBACK / MEANREV)
def pullback_trigger(bar, lv, s):
    return (bar.low <= lv["ref_ma20"] * 1.02
            and bar.close > bar.open
            and bar.close > lv["ref_prev_close"]
            and bar.close > s["session_low"] * 1.005)

# 수급·밸류형 (FLOW_* / VALUE_PURE) — 일봉에서 이미 봤으므로 '안 무너짐'만 확인
def hold_trigger(bar, lv, s):
    return (bar.close > lv["ref_prev_close"]
            and bar.close > s["session_low"] * 1.005
            and bar.close >= bar.vwap)
```

**돌파형에 눌림 확인을 걸면 영영 안 사고, 눌림목형에 돌파 확인을 걸면
비싸게 산다.** 반드시 분리한다.

봉 길이: **판정은 3분봉, 저장은 1분봉.** `BarBuilder`를 둘 돌린다(비용 0).
1분봉을 남겨야 나중에 "1분으로 했으면?"을 데이터로 답한다.

---

## 8. 청산 — 두 갈래

| 종류 | 시점 | 이유 |
|---|---|---|
| 손절 | **틱 즉시** | 봉을 기다리면 더 빠진다 |
| 익절 | 틱 즉시 | |
| 트레일링 | 3분봉 확정 시 | 틱 노이즈 방지 |
| 타임스톱 | 15:20 | |
| 추세이탈 | 15:20 | 일봉 기준이라 마감 판정 |

백테스트에서 배운 것을 반영한다.

- **손절은 넓게.** 고정 20%가 4%보다 22/22에서 나았다. 3~5일 보유에 장중
  저가로 손절을 판정하면 −5%는 거의 확실히 스친다.
- **트레일링은 익절보다 뒤에.** 초기 설정(+5% 발동 / 익절 +12%)에서
  트레일링이 익절을 전부 잡아먹어 이익이 +1.9%에서 잘렸다.

---

## 8-1. 레짐 필터 — ON/OFF 스위치

백테스트에서 **10/11 전략에서 레짐 ON 이 OFF 보다 나았다.** 효과가 가장
컸던 장치다.

```
FLOW_FORGN   ON -11.25% / MDD -41.4%   vs   OFF -27.94% / MDD -76.1%
PULLBACK     ON -22.85% / MDD -65.9%   vs   OFF -41.93% / MDD -88.5%
BREAKOUT     ON -20.56% / MDD -60.6%   vs   OFF -39.83% / MDD -85.0%
```

유일한 예외가 NEWHIGH(OFF 가 +11.7%p 나음)였다. 2022년 신고가 종목이
상대적으로 버텼기 때문이다.

**구현** — `regime.py`

```python
def market_ok(date: str, cfg) -> tuple[bool, float]:
    """(신규 진입 허용 여부, 사이즈 배수)

    지수 종가가 MA 위면 (True, 1.0), 아래면 (False, below_mult).
    below_mult=0.0 이면 신규 진입 전면 중단, 0.5 면 절반 사이즈.
    """
```

- 지수는 코스피(`0001`) 또는 코스닥. 일봉은 야간 배치가 같이 받는다.
- **청산에는 적용하지 않는다.** 레짐이 나빠도 보유분은 정상 청산한다.
- `SWING_REGIME_ENABLED=false` 면 통과만 시킨다.
- 차단된 신호도 `signals` 에 `no_trigger_reason='레짐차단'` 으로 남긴다.
  나중에 "레짐이 얼마나 잘랐나"를 세려면 기록이 있어야 한다.

---

## 8-2. 포지션 사이징

```
종목당 1,000만원 × 5슬롯 = 자본 5,000만원 (균등 배분)
```

```python
shares = int(SWING_POSITION_KRW // entry_px)
```

리스크 기반(손절폭에 반비례)이 아니라 **균등 금액**이다. 백테스트도
`position_pct = 1/max_positions` 균등이었으므로 비교가 가능하다.

> 실전 전환 시 확인 — 기존 대장주봇이 5,000만이므로 합치면 1억이 된다.
> 모의 단계에서는 금액이 자유롭지만 실전에서는 자본 배분을 다시 정해야 한다.

주문 규모 상한도 둔다 — 주문금액이 그날 거래대금의 1%를 넘으면
내 주문이 가격을 민다. `SWING_MAX_ORDER_SHARE=0.01`.

---

## 9. 상태 머신

```
WATCH  → ARMED → ENTERED → HOLDING → EXIT
                     ↑ 체결 확인
  └──────────────────┴─────────→ DROPPED (사유 기록)
```

`DROPPED` 사유를 반드시 남긴다 — 셋업 붕괴 / 레짐 악화 / 미체결 반복 /
슬롯 없음. "왜 감시했는데 안 샀나"를 되짚을 수 있어야 고칠 수 있다.

---

## 10. API 유량 (모의 초당 1건 기준)

```
야간 배치        2,500건 / 42분     ← 장 끝난 뒤, 경쟁 없음
08:45 워밍업        N건             ← 장 시작 전
장중 WebSocket       0건            ← 핵심
장중 주문         2~10건
──────────────────────────────────
장중 REST 소모    10건 미만
```

대장주봇이 장중 REST를 거의 독점할 수 있다.

> 모의와 실전은 계정·approval_key·WS 주소(31000 / 21000)가 모두 다르다.
> **모의로 스윙을 돌리는 동안 실전 대장주봇과 충돌이 아예 없다.**

---

## 11. 실패 처리

| 상황 | 대응 |
|---|---|
| WS 연결 끊김 | 백오프(1→30초) 재연결 + 재등록. 하루 200회 초과 시 중단·알림 |
| 끊긴 구간 틱 | **복구 불가.** `on_gap` → REST 분봉 백필 |
| 등록 거부(한도) | `rejected` 기록, 점수 낮은 종목부터 버림 |
| 일봉 수집 실패 | 실패를 캐시에 남기지 않고 다음 회차 재시도 |
| 야간 배치 실패 | 전일 감시 리스트를 **재사용하지 않는다.** 그날 신규 진입 중단 |
| 장중 크래시 | `positions`에서 HOLDING 복구, WS 재연결 |

---

## 12. 설정 — SWING_* (`.env.overrides`)

> 2026-09-16: 별도 `.env.swing` 폐지. 스톡봇·대장주와 같은 `.env` + `.env.overrides` 를 쓰고 값 정의는
> `stock_bot/config/settings.py` 의 `swing_*` 필드(코드 기본값). `SWING_MODE` 도 없앰 —
> `TRADE_DRY_RUN=true` → dryrun, 아니면 `KIS_ENV` paper→모의 / real→실전 (전역과 동기). 아래는 키 목록·기본값.

```ini
SWING_MODE=dryrun               # dryrun | paper | live
SWING_STRATEGIES=ALL            # 또는 FLOW_FORGN,NEWHIGH,VALUE_PURE
SWING_USE_TREND_FILTER=false    # 추세 전제 제거 (t를 -0.63→+4.16 뒤집음)

# 감시 (한도 41 에 여유를 두고 36만 쓴다)
SWING_WATCH_NEW=30              # 신규 감시
SWING_WATCH_HOLD=6              # 보유 감시 (손절 틱 감시용)
SWING_WATCH_MODE=even           # even | top
SWING_BAR_SEC=180               # 판정용
SWING_BAR_STORE_SEC=60          # 저장용

# 레짐 필터
SWING_REGIME_ENABLED=true
SWING_REGIME_INDEX=0001         # 코스피
SWING_REGIME_MA=200
SWING_REGIME_BELOW_MULT=0.0     # 0.0=신규 전면 중단, 0.5=절반 사이즈

# 진입 시간대 — 기록을 보고 좁힐 것
SWING_ENTRY_FROM=093000
SWING_ENTRY_UNTIL=151500        # 15:20 종가 단일가 이후는 체결 메커니즘이 다름

# 게이트
SWING_ENTRY_MIN_VALUE_EOK=30
SWING_ENTRY_MIN_CAP_EOK=1000
SWING_MAX_ORDER_SHARE=0.01

# 자본·청산
SWING_POSITION_KRW=10000000     # 종목당 1,000만
SWING_MAX_POSITIONS=5           # 5슬롯 → 자본 5,000만
SWING_MAX_NEW_PER_DAY=2
SWING_STOP_PCT=0.20
SWING_TP_PCT=0.12
SWING_TRAIL_AFTER=0.08
SWING_TRAIL_PCT=0.05
SWING_TIME_STOP_DAYS=20
```

---

## 13. 구현 순서와 완료 조건

전부 만든다. 순서는 의존성 때문이지 단계 게이트가 아니다.

| # | 내용 | 완료 조건 |
|---|---|---|
| 0a | WS 한도·분봉 합성 실측 | `test_ws_limit.py` / `test_ws_minute.py` 실행. **한도 숫자 확정** |
| 0b | **KIS 데이터 가용성 확인** | `test_kis_data.py` 실행. 수급·프로그램매매·PER/PBR 이 과거 일별로 오는지 **확정**. 안 되면 pykrx 폴백 결정 |
| 1 | `store.py` | 스키마 생성, 더미 적재·조회 통과 |
| 2 | `collector.py` | 10종목 일봉 **+ 수급 + PER/PBR** 수집 성공, 재실행 시 증분만 |
| 3 | 초기 적재 | 전종목 **400봉** + 수급/밸류 이력 |
| 3b | `dart.py` | 전종목 5년 재무. **`rcept_dt` 가 같이 저장돼야 통과** |
| 4 | `daily_scan.py` | **`bt_swing` 백테스트와 같은 날짜 신호 건수 일치** |
| 5 | `levels.py` + `regime.py` | 기준선 계산, 레짐 ON/OFF 동작 확인 |
| 6 | `triggers.py` | 전략 유형별 트리거, 단위 테스트 |
| 7 | `kis_ws.py` 재작성 | `main.py stream` 동작 유지, 재연결 테스트 |
| 8 | `orders.py` + `exits.py` + `state.py` | 3모드 전부 |
| 9 | `live.py` | WS → 봉 → 트리거 → 주문/기록 전 경로 |
| 10 | `swing_fill_fwd.py` | fwd1/3/5 사후 채우기 |

**4번이 중요하다.** `daily_scan.scan()`이 낸 신호 건수가 `bt_swing`
백테스트의 같은 날짜 신호 건수와 맞아야 한다. 안 맞으면 전략 재사용이
제대로 안 된 것이고, 그러면 실측 결과를 백테스트와 비교할 수 없다.

---

## 14. 무엇을 볼 것인가

### 표본 30건이면 방향이 보이는 것

| 질문 | 보는 법 |
|---|---|
| **분봉 진입이 시가보다 나은가** | `trigger_px < day_open` 비율. **50% 넘으면 기다린 게 이득.** 같은 신호의 두 가격이라 페어 비교 → 표본 적어도 됨 |
| 트리거가 작동하나 | 전략별 타점 발생률. **0%면 트리거가 잘못됐고, 100%면 무의미** |
| 진입 시간대 | `trigger_time` 분포. 09:30 하한이 맞는지 |
| 전략별 신호량 | 감시 배분 근거 |
| 트리거에 변별력이 있나 | 타점난 것 vs 안 난 것의 `fwd3` 차이 |

### 수백 건이 필요한 것

- 어느 전략이 수익성 있나
- 승률이 몇 %인가

**적은 표본의 수익률로 전략을 판단하지 말 것.**

### 백테스트 예측과의 대조 (표본 30~50건이면 가능)

수익률이 아니라 **분포가 재현되는가**를 본다.

| 지표 | 백테스트 예측 (FLOW_FORGN) | 허용 범위 |
|---|---|---|
| 승률 | 약 40% | 30~50% |
| 평균 보유일 | 4.7일 | 3~7일 |
| 손절 비중 | 약 55% | 40~70% |
| 하루 진입 | 1~2건 | 0.5~3건 |

크게 어긋나면 백테스트가 틀렸다는 뜻이다. 비슷하면 백테스트를 믿을 수
있게 되고, 그때 기간 확장(marcap, 2015~)으로 제대로 검증한다.

---

## 15. 아직 안 정해진 것

| 항목 | 상태 |
|---|---|
| WS 한도 | **실측 대기** (20 / 41 / 60 중 뭐가 진짜인지) |
| 장중 종목 교체 | 해제·재등록이 되면 하루 80~100종목 감시 가능 |
| **어느 전략을 돌릴지** | **실측 결과 보고 결정.** 12개 전부 구현해 두고 설정으로 선택 |
| 조건 → 점수 이동 | `--daily-top` 으로 점수 변별력 확인 후 |
| **수급·프로그램매매 소스** | **`test_kis_data.py` 결과 대기.** KIS 로 과거분이 안 되면 pykrx 하이브리드 |
| 프로그램매매 축 | 데이터가 되면 지표·전략 추가 검토. 지금은 수집만 |
| DART 재무 등급 | 3-5절 명세대로 수집·측정. **봇에 얹기 전에 분위·IC 로 값어치부터 확인** |
| 섹터 분산 | 개별 전략은 평균 최대동일업종 1.3~1.5로 자연 분산. 당장 불필요 |
