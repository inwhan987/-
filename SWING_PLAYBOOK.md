# 스윙봇 플레이북 — 구현 기준 전수 정리

`SWING_BOT_DESIGN.md` 는 만들기 전에 쓴 **명세**다. 이 문서는 지금 코드에 **실제로 들어 있는 것**만 적는다.
전략 조건식·점수식·게이트 순서·트리거·청산은 코드에서 그대로 옮긴 것이고, 설정값은 `.env.overrides` 현행이다.
(2026-09-28 기준. 코드가 정본이고 이 문서가 뒤따른다 — 안 맞으면 코드가 맞다.)

---

## 0. 한눈에

```
[전날 15:45] 야간 배치 swing_nightly.py
  유니버스 갱신 → 일봉·수급·프로그램 증분 수집 → 지수 수집
  → 당일 완결 90% 확인 (미달이면 스캔 안 하고 runs=fail)
  → 일봉 스캔: 12전략 조건식 → 원점수 → 진입 게이트 → 재무·프로그램 부착
     → 전략내 백분위(pscore) → 6축 점수 → 종합점수(total_score) → 감시 리스트 저장
  → runs(date,'nightly') = ok/fail        ※ fail 이면 다음날 신규 진입 0건

[당일 08:50] 장중 러너 swing_live.py
  휴장일 판정 → 포지션 복구 → 원장 점유 재등록 → 감시 리스트 적재(+레짐 판정)
  → WS 구독 (보유 + 감시, 최대 41종목)
  → 틱: 손절/익절 즉시 판정
  → 확정봉(3분): 보유면 봉 기준 청산 판정 / 감시면 트리거 판정 → 등급 → 모음 창 → 진입
  → 15:20 EOD: 타임스톱·이평이탈 판정, 미트리거 사유 기록, runs(live)=ok
  → 15:30 WS 종료

[토 03:00] swing_dart_weekly.py — DART 재무 지표 갱신 (기록·축 점수용, 전략 조건 아님)
```

모드는 `mode_of(TRADE_DRY_RUN, KIS_ENV)` 하나로만 갈린다 — `dryrun`(주문 없음, 호출측 가격으로 가상 체결) /
`paper`(KIS 모의) / `live`(실전, `KIS_ENV=real` 일 때만). 별도 `SWING_MODE` 는 없다.
`SWING_TRADE_ENABLED=false` 는 **신규 매수만** 막는다 (보유 청산은 계속 돈다). 장중 핫리드.

---

## 1. 데이터 (swing.db)

`data/swing.db`, SQLite. 테이블:

| 테이블 | 키 | 내용 |
|---|---|---|
| `daily` | (code,date) | 일봉 OHLCV + 거래대금 |
| `meta` | code | 종목명·시장·시총·상장주식수 |
| `flow` | (code,date) | 외국인·기관·개인 순매수 **대금(원)** |
| `fund` | (code,date) | PER·PBR·EPS (KRX 공표치) |
| `program` | (code,date) | 프로그램매매 순매수 수량·대금 |
| `dart_fin` | (date,code) | ROE·부채비율·분기 매출/이익 증가율 + `rcept_dt` |
| `watchlist` | (date,code,strategy) | 감시 리스트 + 기준선(ref_*) + 잠정 손절/익절 |
| `signals` | (date,code,strategy) | 신호·트리거 기록. **왜 안 샀나**(`no_trigger_reason`) 포함 |
| `positions` | id | 포지션 생애 (state, 진입/청산, peak/trough, mfe/mae) |
| `bars` | (code,date,bar_key) | 저장용 분봉(1분). 지우지 않는다 → 보유 종목은 진입일부터 다 남는다 |
| `runs` | (date,kind) | 배치 실행 기록. `nightly` 가 ok 가 아니면 다음날 감시 리스트를 안 쓴다 |

수집은 KIS REST(일봉·수급 30일·당일 PER/PBR·프로그램·지수) + pykrx(유니버스, 과거 수급/밸류).
증분이라 이미 받은 종목은 호출을 건너뛴다. 실패율이 한도를 넘으면 배치를 중단한다(`CollectError`).

---

## 2. 지표 (`bt_swing/indicators.py`)

한 종목 일봉에 붙는 컬럼. 라이브·백테스트가 **같은 모듈**을 쓴다.

- 이평: `ma5/10/20/60/120/200`, `ma20_slope`(5일 변화율), `ma60_slope`(10일), `aligned`(5>20>60), `above_ma200`
- 이격: `disp20 = close/ma20 − 1`, `disp60`
- 변동성: `atr`(14, EWM), `atr_pct = atr/close`, `bb_width = 4σ/ma20`, `bb_width_pct`·`atr_pct_rank`(120일 순위 백분위)
- 위치: `hh20/hh60/hh250`(**당일 제외** 최고가), `ll20`, `near_52w = close/hh250`, `box_pos`
- 모멘텀: `ret5/20/60/120`, `rsi2`, `rsi14`
- 거래: `vol_ma20`, `vol_ratio = vol/vol_ma20`, `value_ma20`, `value_eok = value_ma20/1e8`, `vol_dry`(5일평균/20일평균), `mktcap_eok`
- 캔들: `gap = open/전일close − 1`, `upper_wick`, `body` (둘 다 당일 레인지 기준)
- 수급: `{forgn,inst}_streak`(양수 연속일), `_sum5`, `_sum20`, `_intensity = sum5 / value_ma20`,
  `both_streak = min(두 streak)`, `flow_intensity = forgn_intensity + inst_intensity`
- 펀더: `per`·`pbr`(0→NaN, ffill), `eps_pos`

---

## 3. 전략 12개 — 조건식과 원점수

`bt_swing/strategies.py`. **조건·점수식은 변경 금지 대상**이다. `USE_TREND` 토글이 추세 전제를 `_t()` 로 감싼다.
공통 전제 `_base_gate` = ma20·ma60 존재 & close>0 (유동성·시총은 여기가 아니라 진입 게이트에서 본다).
원점수는 0~100 스케일을 맞춘 것이고, 뒤에 나오는 **종합점수와 다른 축**이다.

| 전략 | 신호 조건 | 원점수 |
|---|---|---|
| **BREAKOUT** | close>hh20 & 양봉 & vol_ratio>1.5 & 전일 bb_width_pct<0.40 & ma20>ma60 & disp20<0.25 | 25·(1−bbw₋₁) + 20·vol_ratio + 20·ret60 + 15·near_52w + 10·ma60_slope + 10·vol_dry₋₁ − 15·disp20 − 10·upper_wick |
| **PULLBACK** | aligned & ma60_slope>0 & 3일 저가 ≤ ma20×1.02 & 양봉 & close>ma20 & disp20∈[−0.02,0.08] & ret20>−0.15 | 40·log(시총) + 30·near_52w + 30·log(거래대금) |
| **NEWHIGH** | close≥hh250 & 양봉 & vol_ratio>1.2 & ma20>ma60 | (30·ret120 + 15·flow_intensity + 15·(1−atr_pct_rank) − 20·disp20) × 100/65 |
| **MOMENTUM** | close ≥ hh60×0.98 & above_ma200 & ret20>0 & ret60>0 & ma20>ma60 | (35·ret60 + 25·ret20 + 20·ma60_slope + 20·(1−atr_pct_rank) − 15·disp20) × 60/70 |
| **MEANREV** | above_ma200 & rsi2<10 & close<ma5 & ret5>−0.20 | (40·log(시총) + 35·(0.07−atr_pct) + 25·vol_ratio) × 0.8473 |
| **GAPGO** | gap∈[0.02,0.12] & 양봉 & vol_ratio>2.0 & ma20>ma60 & close>hh20×0.98 | 30·vol_ratio + 25·body + 20·ret60 + 15·near_52w + 10·flow_intensity − 20·gap |
| **FLOW_FORGN** | forgn_streak≥2 & 전일<2 & forgn_intensity>0.03 & ma20>ma60 | (40·forgn_intensity + 30·near_52w + 30·(1−atr_pct_rank)) × 60/56.6 |
| **FLOW_INST** | inst_streak≥3 & 전일<3 & inst_intensity>0.03 & ma20>ma60 | (50·(1−atr_pct_rank) + 25·(1−disp20) + 25·log(시총)) × 1.31 |
| **FLOW_BOTH** | both_streak≥2 & 전일<2 & flow_intensity>0.05 & ma20>ma60 | 40·log(시총) + 40·(1−atr_pct_rank) + 20·near_52w |
| **FLOW_PULLBACK** | aligned & (forgn_sum20+inst_sum20>0) & flow_intensity>0.02 & 3일 저가 ≤ ma20×1.02 & close>ma20 | 40·(1−atr_pct_rank) + 30·near_52w + 30·log(시총) |
| **VALUE_MOM** | eps_pos & per∈[1,15] & pbr∈[0.2,2.0] & above_ma200 & close>hh20 & vol_ratio>1.3 | 30·(15−per) + 35·(1−atr_pct_rank) + 35·log(시총) |
| **VALUE_PURE** | VALUE_MOM 에서 `close>hh20` 만 뺀 것 | VALUE_MOM 과 동일 |

**현행 가동 8전략** (`SWING_STRATEGIES`): NEWHIGH, MOMENTUM, FLOW_PULLBACK, FLOW_FORGN, FLOW_INST, FLOW_BOTH, MEANREV, GAPGO.
BREAKOUT·PULLBACK·VALUE_MOM 은 2026-09-21 백테스트에서 제외됐고, VALUE_PURE 는 아직 미가동.

NEWHIGH 점수식은 2026-09-22 재설계다 — vol_ratio·ma60_slope 항이 수익과 **음의 상관**(ρ −0.13 / −0.07)이어서 뺐다.
점수-수익 상관이 +0.08 → +0.18 로 올랐고 5슬롯 수익이 +152% → +213%.

---

## 4. 진입 게이트 (야간, `daily_scan._entry_gate`)

신호가 떠도 이걸 못 넘으면 감시 후보가 아니다. **순서대로** 판정하고 첫 탈락 사유를 기록한다.

1. `전략별 원점수 하한` (`SWING_ENTRY_MIN_RAW_BY_STRATEGY`) 미달 → `점수미달`
2. `value_eok < SWING_ENTRY_MIN_VALUE_EOK` → `유동성미달`
3. 시총 NaN → `시총불명` / `< SWING_ENTRY_MIN_CAP_EOK` → `시총미달`
4. `close < SWING_ENTRY_MIN_PRICE` → `가격미달`
5. `atr_pct > SWING_ENTRY_MAX_ATR_PCT` → `과열`

현행: 원점수 하한 `PULLBACK:80, FLOW_FORGN:70, GAPGO:80, VALUE_MOM:70, VALUE_PURE:70` (나머지 컷 없음),
거래대금 30억, 시총 **5000억**, 주가 1000원, ATR **7%**.

- 시총 1000→5000억 (2026-09-23): 10전략 중 8개가 5000억 미만 구간에서 PF<1.
- ATR 15%→7% (2026-09-21): 7~10% 구간 PF 1.09, 10~15% 구간 PF 0.63.

---

## 5. 점수 체계 — 원점수 → 백분위 → 6축 → 종합

세 단계가 다 다른 값이다. 혼동 주의.

### 5-1. 전략내 백분위 `pscore`
그 날 **그 전략의 모든 신호**(게이트 통과 여부 무관) 안에서 원점수의 백분위.
그날 표본이 `SWING_PSCORE_MIN_N`(30) 미만이면 최근 `SWING_PSCORE_POOL_DAYS`(20) 스캔일 점수를 합쳐서 잰다.
전략마다 점수 스케일이 달라 원점수를 직접 비교할 수 없기 때문이다.

### 5-2. 6축 점수 (`swing/axes.py`)
재료를 백분위로 바꾼 뒤 축별로 평균. 재료가 없으면 그 축은 **NULL** (50 으로 메우지 않는다).

| 축 | 재료 (부호) |
|---|---|
| `value` | per(−), pbr(−) |
| `quality` | roe(+), 부채비율(−) |
| `growth` | 분기 매출증가율(+), 분기 이익증가율(+) |
| `flow` | flow5_r(+), flow20_r(+) — 순매수 누적 ÷ value_ma20 |
| `liq` | value_ma20(+), turnover(+) — turnover = value_ma20 ÷ 시총 |
| `prog` | prog5_r(+) — 프로그램 5일 순매수 ÷ value_ma20 |

재무는 `dart_fin` 에서 `rcept_dt ≤ 스캔일` 인 것만 쓴다 (미래 정보 차단).

### 5-3. 종합점수 `total_score`
`setup_pscore` 와 **존재하는 축들**의 평균. 행(code×전략) 단위. 다 없으면 NaN.
`RANK_BASIS = "total_score"` 이고, `rank_overall` 은 게이트 통과분 안에서의 종합점수 순위다.

> 종합점수는 **랭킹·컷에만** 쓰인다. 2026-09-26 검증에서 근사 점수 랭킹은 무작위 컷과 구분되지 않았다
> (12월 이후 실DB 로 재검증 예정). 컷으로서는 유효.

---

## 6. 감시 리스트 (`daily_scan.build_watchlist`)

1. 게이트 통과분만
2. 종목당 대표 전략 1개 (종합점수 최고, 동점이면 pscore)
3. `SWING_WATCH_POOL`(50) 상위까지 자름
4. **축 하한**: 존재하는 축 중 하나라도 `SWING_AXIS_MIN_EACH`(20) 미만이면 탈락 (NULL 축은 무시)
5. 배분 `SWING_WATCH_MODE`
   - `even`(현행): 전략별로 자리를 균등 배분, 나머지는 종합점수 순
   - `top`: 종합점수 순으로만
6. 요청 개수 = `SWING_WATCH_NEW`(30) + `SWING_WATCH_HOLD`(10) — 보유 종목이 감시 자리를 먹지 않게 여유를 둔다

### 기준선 (`swing/levels.py`) — 장중 트리거가 쓰는 값
스캔 당일 일봉에서 뽑아 `watchlist` 에 박아둔다.

- `ref_ma20`, `ref_ma60`
- `ref_prev_close` = 신호일 종가
- `ref_box_top` = 20일 최고가 (**신호일 포함**)
- `ref_atr_pct`
- `ref_avg_bar_vol` = vol_ma20 ÷ (하루 봉 수) — 하루 390분 ÷ `SWING_BAR_SEC`
- `stop_px` / `tp_px` — 신호일 종가 기준 **잠정**. 체결가가 나오면 `entry_levels(체결가, rule)` 로 다시 계산해 덮는다
- `ref_value_ma20`, `ref_mktcap` — 주문 규모 캡·로그용

---

## 7. 타점 — 장중 트리거 (`swing/triggers.py`)

확정봉(`SWING_BAR_SEC`, 현행 3분)마다 판정. 전략별로 세 유형 중 하나(`TRIGGER_KIND`).

### 7-1. `breakout` — BREAKOUT, NEWHIGH, GAPGO, VALUE_MOM
```
close > ref_box_top           아니면 "박스미돌파"
volume > ref_avg_bar_vol×1.5  아니면 "거래량부족"
기준선 없음 → "기준선없음"
```

### 7-2. `pullback` — PULLBACK, MEANREV
```
low ≤ ref_ma20×1.02       아니면 "미눌림"
close > open              아니면 "음봉"
close > ref_prev_close    아니면 "전일종가하회"
close > 당일 저가×1.005    아니면 "저점근접"
```

### 7-3. `hold` — FLOW_PULLBACK, FLOW_FORGN, FLOW_INST, FLOW_BOTH, VALUE_PURE, MOMENTUM
```
close > ref_prev_close    아니면 "전일종가하회"
close > 당일 저가×1.005    아니면 "저점근접"
close ≥ vwap (있으면)      아니면 "VWAP하회"
```

FLOW_PULLBACK 은 2026-09-22 에 breakout → hold 로 옮겼다. D+1 재눌림 트리거가 **12% 만 발동**하고 그마저 손실이었다.

트리거가 안 잡히면 사유를 `last_reason` 에 남기고, 15:20 에 `signals.no_trigger_reason` 으로 저장한다.
"왜 안 샀나" 가 항상 남는다.

---

## 8. 진입 — 등급 · 모음 창 · 사이징 (`swing/live.py`)

### 8-1. 등급 (우선 / 일반)
- 종합 ≥ `SWING_PRIORITY_SCORE`(80) → **우선**: 트리거 즉시 진행
- `SWING_ENTRY_MIN_SCORE`(60) ~ 80 → **일반**: 다음 `SWING_NORMAL_CONFIRM_BARS`(1)봉 유지 확인 후 진행
  (다음 봉에서 트리거가 깨지면 `다음봉미유지` 로 취소)
- 우선 등급이 하나도 없으면 감시 순위 1~`SWING_PRIORITY_FALLBACK_RANK`(10) 을 우선으로 취급
- 일반 등급 하루 한도 = `SWING_MAX_NEW_PER_DAY × SWING_NORMAL_MAX_RATIO` (내림, 최소 1) — 현행 3×0.5 = **1건**
  남는 자리는 우선 등급 몫이고, 안 채워지면 그냥 비운다

### 8-2. 진입 차단 사유 (`_entry_allowed`, 순서대로)
`시간전`(<09:30) → `시간후`(>15:15) → `매수OFF`(SWING_TRADE_ENABLED=false) → `레짐차단`
→ `슬롯 없음`(공용 슬롯 소진) → `일일한도`(3건) → `일반한도`
그 뒤 종합점수 < 60 이면 `점수보류`.

### 8-3. 모음 창
같은 봉에 여러 트리거가 뜨면 `SWING_ENTRY_BATCH_SEC`(20초) 동안 모아뒀다가
**우선 등급 먼저, 그 안에서 종합점수 높은 순**으로 하나씩 진입한다. 한 건씩 슬롯·한도를 다시 확인한다.
진입 순서는 디스코드로도 알린다.

### 8-4. 사이징
- 슬롯 금액 = `STOCK_BUDGET_KRW ÷ STOCK_MAX_POSITIONS × size_mult` — **단타봇과 공용**, 장중 핫리드.
  현행 5000만 ÷ 10 = **500만원/슬롯**
- 주문 규모 캡: `SWING_MAX_ORDER_SHARE`(1%) × ref_value_ma20 를 넘지 않게 — 한산한 종목은 슬롯보다 작게 들어간다
- 0주면 `주문규모` 로 미진입
- 슬롯 사용량은 **공용 원장**(`ledger.shared_used`)으로 센다. 다른 봇이 이미 잡은 종목이면
  `점유충돌` 로 오늘 포기하고 감시에서 빼고 예비 후보를 승격한다 (더블 매수 방지)

### 8-5. 체결 확정 (`swing/orders.py`)
시장가로 내고 끝내지 않는다. 대장주봇에서 이식한 절차:

1. 시장가 → 체결 조회. 잔량이 0인데 목표에 못 미치면 **부족분 재주문** (최대 3회, 1초 간격)
2. 잔량이 살아있거나 판정 불가면 재주문 금지 — **계좌 잔고**가 목표를 채우거나 예산(`SWING_FILL_BLOCK_SEC`, 기본 20초)이 다할 때까지 지켜본다
3. 예산 소진 → 잔량 취소 → 잔고 변화분으로 체결 수량 확정 (유령 잔량 차단)
4. 마지막에 잔고와 한 번 더 대조해 늦은 체결을 흡수

`filled` 은 전량이 아니라 **1주 이상**. 부분·초과 체결은 실제 수량으로 포지션을 기록하고 알림을 띄운다.
잔량 취소 실패는 🚨 로 반드시 드러낸다(HTS 확인 필요).
`mode` 와 `KIS_ENV` 가 어긋나면 주문 없이 `SystemExit` — 실계좌 토글은 사용자만 바꾼다.

### 8-6. 감시 자리 회전
`_watch_cap` = `SWING_WATCH_NEW` − max(0, 보유 − `SWING_WATCH_HOLD`).
체결·미체결·점유충돌로 자리가 비면 예비 후보를 승격하고 장중 구독을 추가한다.
WS 구독 한도(41)에 걸리면 승격을 보류한다. 끝까지 자리가 안 난 예비는 15:20 에 `슬롯 없음` 으로 기록.

---

## 9. 청산 (`swing/exits.py`)

판정 순서는 백테스트 엔진과 같다: **손절 → 익절 → 트레일링 → 타임스톱 → 이평이탈**.
레짐은 청산에 **절대 관여하지 않는다**.

| 시점 | 판정 |
|---|---|
| **틱** (`check_tick`) | `price ≤ stop_px` → 손절 / `price ≥ tp_px` → 익절 |
| **확정봉** (`check_bar`) | peak·trough 갱신(MFE/MAE) → `low ≤ stop_px` → 손절(stop_px 로) → `high ≥ tp` → 익절(tp 로) → 트레일링 |
| **15:20** (`check_eod`) | `보유일 ≥ time_stop_days` → 타임스톱 / `종가 < n일선` → `{n}일선이탈` |

트레일링: `high ≥ 진입×(1+trail_after)` 에서 무장(`trail_on=1`), 이후 `close ≤ peak×(1−trail_pct)` 에서 발동.
보유일은 지수 일봉 달력으로 센다(진입 당일 = 0). 이평은 전일까지 n−1개 + 당일 종가.

### 청산 규칙 (`ExitRule`)
`stop / tp / trail_after / trail_pct / time_stop_days / ma_exit`, 라벨은 `stopX/tpY/trailA>B/tN/maM`.
전략별로 다르게 줄 수 있다 — `SWING_EXIT_BY_STRATEGY` = `전략:키=값,...;전략:...` (키: stop/tp/trail_after/trail/time/ma).
안 준 키는 공통 기본값을 물려받고, 모르는 키가 있으면 그 덩어리 전체를 경고 후 무시한다.

**현행(2026-09-21 통일)**: 손절 **20%**, 익절 없음, 트레일 없음, 이평이탈 없음, 타임스톱 **30일**.
8전략 전부 같은 값이다. 전략별 차별화는 2026-09-27 검증에서 IS만 개선·OOS 악화로 기각됐고, **익절은 해롭다**.

청산 실패는 포지션을 살려두고 다음 틱에 재시도한다. 부분 청산은 잔여 수량으로 포지션을 유지한다.

---

## 10. 레짐 (`swing/regime.py`)

`SWING_REGIME_INDEX`(0001=KOSPI) 종가 vs MA(`SWING_REGIME_MA`, 200).

- 위 → (통과, mult 1.0)
- 아래 → (차단, mult `SWING_REGIME_BELOW_MULT`) — 현행 0.0 이므로 **신규 진입 0건**, 감시는 기록용으로 유지
- 비활성/봉 부족 → 경고 로그와 함께 통과

게이트 우회는 없다. 청산에는 적용되지 않는다.

---

## 11. 상태 머신 (`swing/state.py`)

```
WATCH → ARMED → ENTERED → HOLDING → EXIT
              ↘ DROPPED (셋업 붕괴 / 레짐 악화 / 미체결 반복 / 슬롯 없음 / 점유충돌)
```

허용 전이만 기록된다: ARMED→{ENTERED,DROPPED}, ENTERED→{HOLDING,DROPPED}, HOLDING→{EXIT}.
`exit_()` 에서 peak/trough 대비 `mfe_pct`·`mae_pct` 를 남긴다.

### 재시작 복구 (`_recover_positions`)
- `HOLDING` 은 그대로 살린다
- 전날 `ARMED` 잔존 → DROPPED
- `ENTERED` → 계좌 잔고 조회로 판정. 잔고 있으면 HOLDING 으로 승격(손절선 재계산), 없으면 DROPPED
- 잔고 조회 자체가 실패하면 보유로 간주해 **손절은 돌린다** (판단 보류)
- 오늘 신규 건수·일반 등급 건수도 DB 에서 되살린다 (재시작이 한도를 리셋하지 않게)

### 감시 리스트 재사용 조건 (`_load_watchlist`)
`runs(wl_date,'nightly').status == 'ok'` 이고, wl_date 가 오늘보다 앞이고 최근 일봉일보다 뒤여야 한다.
하나라도 어긋나면 **신규 진입 없음**. 야간 배치 실패가 조용히 묵은 리스트로 사는 걸 막는다.

---

## 12. 장중 관측·안전장치

- **WS 끊김**: KIS 모의는 매시 정각에 서버가 끊는다(실측). 재접속을 먼저 시키고 REST 1분봉 백필은
  백그라운드 태스크로 45초 뒤에 돈다 — 보유 종목 먼저, 세션 고저·peak/trough 를 갱신. 백필 **실패만** 알린다.
- **틱 무수신 감시**: 장중에 `max(120초, 2×BAR_SEC)` 넘게 틱이 없으면 경고(5분 쿨다운).
- **하트비트**: 60초마다 `runs(live)='running'` — 웹 대시보드 '장중' 배지.
- **모니터 한 줄**: WS 상태·감시/보유/신규 건수·대기·확인중·진입가능 여부·미트리거 사유 Top5 를 주기적으로 로그.
- **WS 조기 종료**: 15:20 전에 끊기면 "손절/익절 감시가 멈췄다" 고 ⚠️ 알림.
- **휴장일**: 대장주·단타와 같은 판정 모듈(KIS 국내휴장일 API → 수동보강 → exchange_calendars → 주말).
- **분봉 저장**: `SWING_BAR_STORE_SEC`(60초)로 별도 저장. 지우지 않으므로 보유 종목은 진입일부터의 분봉이
  전부 남고, 웹 차트에서 날짜를 골라 진입 타점까지 볼 수 있다.

---

## 13. 현행 설정값 (`.env.overrides`)

```
SWING_TRADE_ENABLED=true          SWING_DART_ENABLED=true
SWING_WATCH_NEW=30                SWING_WATCH_HOLD=10           SWING_WATCH_POOL=50
SWING_AXIS_MIN_EACH=20            SWING_PSCORE_MIN_N=30         SWING_PSCORE_POOL_DAYS=20
SWING_MAX_NEW_PER_DAY=3           SWING_ENTRY_MIN_SCORE=60      SWING_PRIORITY_SCORE=80
SWING_PRIORITY_FALLBACK_RANK=10   SWING_NORMAL_CONFIRM_BARS=1   SWING_NORMAL_MAX_RATIO=0.5
SWING_ENTRY_FROM=09:30            SWING_ENTRY_UNTIL=15:15
SWING_ENTRY_MAX_ATR_PCT=0.07
SWING_STRATEGIES=NEWHIGH,MOMENTUM,FLOW_PULLBACK,FLOW_FORGN,FLOW_INST,FLOW_BOTH,MEANREV,GAPGO
SWING_STOP_PCT=0.20  SWING_TP_PCT=0  SWING_TRAIL_AFTER=0  SWING_TRAIL_PCT=0
SWING_TIME_STOP_DAYS=30           SWING_EXIT_TREND_BREAK=false
SWING_EXIT_BY_STRATEGY=<8전략 전부 stop=0.20,tp=0,trail=0,ma=0,time=30>
STOCK_BUDGET_KRW=50000000         STOCK_MAX_POSITIONS=10        (단타 공용)
```

오버라이드에 없어서 코드 기본값이 그대로 쓰이는 것:
`SWING_ENTRY_MIN_VALUE_EOK=30`, `SWING_ENTRY_MIN_CAP_EOK=5000`, `SWING_ENTRY_MIN_PRICE=1000`,
`SWING_ENTRY_MIN_RAW_BY_STRATEGY=PULLBACK:80,FLOW_FORGN:70,GAPGO:80,VALUE_MOM:70,VALUE_PURE:70`,
`SWING_BAR_SEC=180`, `SWING_BAR_STORE_SEC=60`, `SWING_WATCH_MODE=even`, `SWING_ENTRY_BATCH_SEC=20`,
`SWING_MAX_ORDER_SHARE=0.01`, `SWING_REGIME_*`(on/0001/200/0.0).

> **파라미터는 5곳이 같아야 한다**: `.env.overrides` / `stock_bot/config/settings.py` 기본값 /
> `stock_bot/live/runner.py` 핫리드 목록 / `swing/config.py` / 웹 파라미터 페이지.
> 웹에서 저장만 하면 `update.sh` 가 origin 값으로 되돌린다 — `.env.overrides` 가 정본이다.

---

## 14. 검증에서 나온 것 / 기각된 것

지금 파라미터가 왜 이 값인지의 근거. **다시 제안하지 않는다.**

**채택**
- 시총 5000억 게이트 + 전략별 원점수 하한 (2026-09-23, eecbd59)
- ATR 게이트 7% (2026-09-21)
- NEWHIGH 점수식 재설계 (2026-09-22)
- FLOW_PULLBACK 트리거 breakout → hold (2026-09-22)
- 청산 통일: 손절 20%, 익절·트레일·이평이탈 없음, 타임스톱 30일 (2026-09-21)

**기각 (근거 있음)**
- 익절 추가 / 빠른 익절-손절 / 작은 TP — **익절은 해롭다**
- 전략별 청산 차별화 — IS 개선, OOS 악화
- 타임스톱 연장(40일 초과) — 패널 45봉 한계에 걸린 함정이었다
- 손절 축소(5% 이하) — 승률은 손절을 **넓혀야** 오른다
- 종합점수로 랭킹 — 무작위 컷과 구분 안 됨 (컷으로만 유효)
- 단기 모멘텀 신규 전략 — 역엣지
- 백지 재설계 — 신고가 엣지 하나만 생존했으나 기존 대비 열위
- 그 외: ATR 손절, 구조 손절, 추세 청산, 정체 청산, 눌림 지정가 진입, 축 재조합, 패널 재구축,
  종목별 전략 적합도, kNN 유사사례, 추가 지수 필터, 단일 전략 통합

**관측 대기**
- 겹침 우선순위(여러 전략 동시 신호 우선) — 2026년엔 +62→+100%, 2024엔 무효. 검증 대기
- 변동성 사이징 1/ATR — 리스크만 줄이는 쪽, 미적용
- 슬롯 수는 **엣지가 아니라 레버리지**다. 슬롯을 늘리면 수익과 최악월 손실이 같이 커진다

**트레일링**은 MDD·월 승률만 개선한다(총수익은 아니다). 필요하면 그 목적으로만 켠다.

---

## 15. 실행 방법

```bash
# 야간 배치 (수집 + 스캔)
python scripts/swing_nightly.py [--date YYYYMMDD] [--budget-sec N] [--no-collect]

# 장중 러너
python scripts/swing_live.py [--date YYYYMMDD]

# 주간 DART
python scripts/swing_dart_weekly.py
```

파이에서는 상주하지 않는다. 호스트 crontab 이 매번 `docker compose run --rm swing-bot ...` 로 띄우고 끝나면 내려간다
(`ops/swing.crontab`): 평일 08:50 live / 평일 15:45 nightly / 토 03:00 DART.
컨테이너 `mem_limit` 500m — 나이틀리가 전종목 400봉 패널을 올리므로 매매봇을 굶기지 않게 캡을 걸어뒀다.
초과하면 스윙만 죽고 `runs=fail` 이 남아 다음날 신규 진입이 0건이 된다(설계된 안전 방향).
