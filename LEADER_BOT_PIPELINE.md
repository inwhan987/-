# 대장주봇 전체 파이프라인 — 선별부터 타점까지

> 스냅샷 기준: 2026-08-16, 파이 `/api/params` GET 실측값 (`params_now.json`).
> 코드 기준: `leader_finder.py` (선별) + `stock_bot/live/leader_trader.py` (진입/청산/회전).

## ⚠️ 메모리와의 불일치 (중요)

기존 메모리(`current_params.md`, 2026-08-12 스냅샷)에는 다음과 같이 기록되어 있으나, **오늘 실측한 라이브 값과 다릅니다**:

| 파라미터 | 메모리(2026-08-12) | 라이브 실측(2026-08-16) |
|---|---|---|
| `LEADER_ENTRY_MODE` | `or_mode` | **`vwap_touch`** |
| `TRADE_DRY_RUN` | `false` (실매매) | **`true`** (모의) |

이 문서는 **라이브 실측값을 기준**으로 작성했습니다. 메모리 쪽이 오래돼서 그런 것인지, 실제로 두 값을 되돌린 것인지 확인이 필요합니다.

---

## 0. 두 단계 구조 개요

```
[1단계] leader_finder.py  (매일 아침 신규 프로세스로 실행, run_once)
   유니버스 랭킹 → 자격필터(4단 게이트) → 핫테마 탐지 → 종목/섹터 점수화
   → 섹터별 top1(+top3 기록) → picks JSON 저장 (data/leader_picks/YYYY-MM-DD.json)

[2단계] leader_trader.py  (장중 상시 실행, tick / check_exit_fast)
   picks JSON 로드 → 바스켓 구성(60%룰) → 3분봉 감시
   → VWAP터치/눌림목 신호 판정 → 진입 → 분할/트레일링/고정 청산
   → 13:00까지 섹터 재평가·전환(회전)
```

---

## 1단계: 종목·섹터 선별 (`leader_finder.py`)

### 1-1. 데이터 소스

- 순위(등락률/거래대금) 랭킹: **다음(Daum)** 1순위 → 실패 시 **KIS** 폴백
- 5일 평균 거래대금(`avg_value_5d`): **KIS J(KRX) 일봉** 1순위 → **pykrx** 폴백 → 재시도 실패 시 캐시(stale) 폴백. 2026-08-10부터 KRX-only 정책(넥스트레이드 분리 문제 회피, [[leader_universe_kis]])
- 섹터 소속: 네이버 coinfo
- 시가총액: 매경 캐시에서 주입

### 1-2. 자격 필터 (4단 게이트, 순서대로 적용)

`run_once()`가 거래대금 상위 `LEADER_SEL_TOP=100`개 종목을 뽑은 뒤, 다음 게이트를 순서대로 통과해야 살아남습니다. 각 게이트에서 탈락한 개수를 퍼널 카운터로 기록합니다.

**게이트 A — 상승률 하한**
```
change_pct >= LEADER_SEL_RISE_MIN (5%)
```

**게이트 B — 상승률 상한 (과열 컷)**
```
change_pct <= LEADER_SEL_MAX_CHANGE (25%)
```

**게이트 C — 동적 최소 거래대금 (시장별, 시간·수급 연동)**

가장 복잡한 게이트입니다. 시장(KOSPI/KOSDAQ)별로 다른 최소 거래대금 임계값을 매 실행마다 새로 계산합니다.

1. **시장 흐름 배수** (`_compute_intraday_flow_multiplier`):
   ```
   flow_mult = clamp(오늘 상위N 거래대금 합 / 과거 평균 거래대금 합,
                      LEADER_MF_CLAMP_LOW=0.5, LEADER_MF_CLAMP_HIGH=1.5)
   ```
   장이 평소보다 활발하면 배수↑(임계값↑, 더 엄격), 한산하면 배수↓(더 관대).

2. **선별창 진행률** (`_pick_fraction`, 09:00~`LEADER_SWITCH_UNTIL`(13:00) 창 기준, 전체 장 fraction과 별도 스케일):
   ```
   pick_window_min = (13:00 - 09:00) = 240분
   elapsed_min = 지금 - 09:00
   pick_fraction = max(elapsed_min / pick_window_min, 0.02)   # 최소 2% 바닥
   ```

3. **앵커 기준 시간 대비 시간 스케일**: `LEADER_SEL_MIN_VALUE_ANCHOR_HHMM=11:00`을 기준점(그 시각에 `LEADER_SEL_MIN_VALUE_EOK`=500억이 되도록)으로 선형 스케일:
   ```
   anchor_fraction = (11:00 - 09:00) / 240분 = 0.5
   time_scale = pick_fraction / anchor_fraction
   raw_threshold = LEADER_SEL_MIN_VALUE_EOK(500억) * time_scale * flow_mult
   ```
   즉 아침 일찍(09:30경)일수록 임계값이 낮고, 정오에 가까울수록 500억에 수렴, 오후로 갈수록 커짐.

4. **최종 클램프**:
   ```
   min_value_by_market[시장] = clamp(raw_threshold,
                                       LEADER_SEL_MIN_VALUE_FLOOR_EOK=150억,
                                       LEADER_SEL_MAX_VALUE_EOK=800억)
   ```

게이트 조건: `당일 거래대금 >= min_value_by_market[시장]`

**게이트 D — 시가총액 하한 + 회전율 상한**
```
market_cap >= LEADER_SEL_MIN_CAP_EOK (1000억)
turnover_pct = 거래량 / 상장주식수  (또는 시총/가격으로 유추)
turnover_pct <= LEADER_SEL_TURNOVER_CAP_PCT (200%)
```
회전율 상한은 품절주/작전주성 과열 종목을 걸러내기 위한 컷.

### 1-3. 핫테마(섹터) 탐지

자격 통과 종목들을 섹터별로 묶고, 다음 조건을 만족하는 섹터만 "핫섹터" 후보:
```
섹터 내 자격통과 종목 수 >= LEADER_SEL_HOT_MIN (3개)
평소 대비 거래대금 배수 >= LEADER_SEL_VOL_MULT (2배)
```
`THEME_EXCLUDE = ("밸류업", "value-up", "value up")` 섹터는 테마 취급에서 제외(가치주 리밸런싱성 상승은 대장주 로직과 성격이 다름).

같은 테마가 이름만 다르게 여러 섹터로 잡히는 경우, **50% 이상 종목 중복 시 동일 테마로 판단**하여 중복 제거(dedup).

### 1-4. 종목 점수 (`stock_score`)

절대 앵커(absolute anchor) 방식 — 백분위(percentile)가 아님. n=3 소규모 바스켓에서 percentile을 쓰면 절대값이 비슷해도 억지로 [1, 0.5, 0]으로 벌어지는 문제가 있어 2026-08-11부로 절대 기준 정규화로 전환.

각 성분의 정규화 공식:

| 성분 | 정규화 공식 | 의미 |
|---|---|---|
| `pc_lv` (거래대금) | `_abs_log_value`: log10(거래대금) 기준, 100억=0, 5000억=1 | 로그 스케일 |
| `pc_nb` (수급) | 항상 0 (2026-08-11 KIS 개별종목 실시간 수급 미제공 확인, 기능 삭제) | 비활성 |
| `pc_chg` (등락률) | `_abs_change`: 5%=0, 30%=1 (선형) | 상승 강도 |
| `pc_to` (회전율) | `_abs_turnover`: 0%=0, 30%=1 (선형) | 수급 쏠림 |
| `pc_vr` (거래량배수) | `_abs_vol_ratio`: 로그 스케일, 1배=0, 30배=1 | 평소 대비 급증 |

가중치(라이브 실측, `_stock_weights`가 `lead_st_w_*` 읽음):
```
W_VALUE    = LEAD_ST_W_VALUE    = 0.2
W_UPDN     = LEAD_ST_W_UPDN     = 0.45   (등락률, 가장 큰 비중)
W_TURNOVER = LEAD_ST_W_TURNOVER = 0.15
W_SURGE    = LEAD_ST_W_SURGE    = 0.2    (거래량배수)
netbuy 슬롯 = 0.0 (하드코딩, 수급 삭제됨)
```
가중치 합이 1.0이 아니어도 `_normalize()`가 자동으로 합=1.0이 되게 재조정합니다.

```
stock_score = pc_lv*W_VALUE + pc_chg*W_UPDN + pc_to*W_TURNOVER + pc_vr*W_SURGE
            (정규화 후 4개 가중치 합 = 1.0)
```

### 1-5. 섹터 점수 (`sector_score`)

```
intensity = mean(섹터 내 stock_score 들)
breadth   = mean(stock_score) / max(stock_score)   # 상승이 얼마나 고르게 분산됐는지
sector_score = intensity * (W_INTENSITY + W_BREADTH * breadth)
```
가중치(라이브): `LEAD_SC_W_INTENSITY=0.65`, `LEAD_SC_W_BREADTH=0.35`.

`LEADER_SEL_SECTOR_TOP3=true`이면 섹터 내 상위 3종목만 점수 계산에 포함(전체가 아님).

`breadth`가 1에 가까울수록(고르게 분산) 가점, 특정 1종목만 튀면(mean≪max) breadth가 작아져 감점 — "섹터 전체가 움직이는가"를 반영하는 장치.

### 1-6. 최종 리더 선정

- 각 핫섹터에서 `stock_score` 최고 종목 = 그 섹터의 "1등"(top1), 2·3등도 `top3` 배열에 기록(점수 포함)
- 섹터들을 `sector_score` 내림차순 정렬
- (선택) `LEADER_DAILY_TREND_GATE`가 켜져 있으면 일봉 추세 게이트 추가 적용 — **현재 라이브값 `false`(비활성)**. 참고로 `daily_trend_of`(Minervini-lite 일봉 추세 평가)는 관찰용으로만 계산되며 기본적으로 선별/진입 어느 쪽에도 영향 없음.
- 결과를 picks JSON(`data/leader_picks/YYYY-MM-DD.json`)에 저장 — 각 섹터 항목에 `top3`(rank/code/name/change_pct/stock_score), `sector_score` 포함.

---

## 2단계: 바스켓 구성 — 60% 밴드룰 (`_build_basket`, `leader_trader.py:134`)

picks JSON에서 섹터를 읽어 실제 진입 감시 대상(바스켓)을 만들 때 공통으로 쓰는 규칙:

```
lead_sc = top3[0].stock_score   # 그 섹터 1등 점수
if lead_sc > 0:
    basket = [ m for m in top3 if m.stock_score >= lead_sc * LEADER_BAND_RATIO(0.6) ]
else:
    basket = [top3[0]]   # 구 포맷(점수 없음) 대비 보수적 1등만
```

즉 섹터 내 2·3등도 1등 점수의 **60% 이상**이면 같이 감시 바스켓에 포함됩니다(2026-08-10 "60%룰 통일"로 섹터·종목 레벨 모두 동일 비율 적용, [[leader_sector_improve]]).

`LEADER_OWN_SYMBOL_PRIORITY=true`(라이브)이면 스톡봇(`SYMBOLS`)이 잡은 종목을 대장주봇에서 배제하지 않음(점유락으로 상호배제, 대장주가 스톡봇 미보유 종목을 자유롭게 잡을 수 있게).

이 60%룰은 세 군데에서 반복 적용됩니다:
1. **`_load_day`(초기 로드)**: 정본 1등 섹터의 top3에 적용 + `LEADER_MAX_SECTORS=3`개까지 추가 섹터를 `sector_score >= top_score*0.6` 조건으로 동시 시딩(§4-2 첫 선별 다중섹터)
2. **`_reval_resort`(장중 재평가)**: 13:00까지 지속 재적용, 보유+신규 후보를 합쳐 재정렬
3. **`_summary_text`(디스코드/웹 알림 미리보기)**: 동일 로직으로 통과/컷 표시(✅/❌/⚖️)

---

## 3단계: 진입 신호 판정 (`_check_signal`, `leader_trader.py:640~1007`)

3분봉(`LEADER_INTERVAL_MIN=3`) 종가마다 바스켓 내 종목을 스캔합니다. `LEADER_ENTRY_MODE=vwap_touch`(라이브)이므로 아래 VWAP터치 신호만 사용되고, 눌림목(or_mode) 폴백 로직은 **현재 비활성**입니다.

### 3-1. VWAP-터치 신호 (`_signal_vwap_touch`) — 현재 라이브 모드

세션 VWAP(09:00부터 누적, 거래량가중 typical price):
```
VWAP = Σ(typical_price_i * volume_i) / Σ(volume_i)
typical_price = (고가+저가+종가)/3
```

3개 조건을 모두 만족해야 신호 발생:
```
(a) 상승추세:   이전 봉 종가 > 이전 봉 VWAP
(b) 터치:       이번 봉 저가 <= VWAP * (1 + LEADER_VWAP_TOL/100)   # 0.3% 허용오차
(c) 회복(리클레임): 이번 봉 종가 >= VWAP
```

공통 컷 (VWAP·눌림목 신호 모두 적용):
- **장대양봉컷**: `(고가-저가)/저가*100 <= LEADER_BAR_RANGE_PCT (1.5%)` — 이 봉이 이미 너무 크게 움직였으면 진입 스킵(추격매수 방지)
- **상한가컷**: 목표 익절가가 `전일종가 * 1.30`(상한가)을 넘어서면 진입 스킵

### 3-2. 눌림목/or_mode 폴백 신호 (현재 라이브에서 미사용, 참고용)

`LEADER_ENTRY_MODE=or_mode`일 때만 VWAP 신호가 없을 경우 시도되는 대체 로직:

1. **동적 앵커**(`LEADER_ANCHOR=vwap`, off/ema/vwap/both 중): 스윙저점이 이 앵커선 위에서 형성되어야 함. EMA 사용 시 `LEADER_ANCHOR_EMA=20`.
2. **Phase 1 전고점 탐색**: `LEADER_PHWIN_MIN=30`분 윈도우(0이면 09:00부터) 내 최고가를 전고점으로.
3. **바닥(floor) 계산**:
   ```
   floor = pre_high * (1 - LEADER_MAX_PULL_PCT/100)     # 고정 눌림폭, 기본
   # LEADER_FIB_DYNAMIC=true 이고 LEADER_FIB_PCT>0 이면 피보나치 되돌림 floor로 대체
   ```
   라이브: `LEADER_FIB_DYNAMIC=false`, `LEADER_FIB_PCT=0.0` → 고정 눌림폭(5%) 사용.
4. **Phase 2 스윙저점 탐지**: 좌우 `LEADER_W=2`봉 윈도우에서 저점이 `floor` 이상이어야 유효.
5. **붕괴컷**: 전고점 이후 진입 전에 가격이 `floor`를 이미 이탈했으면 그날 해당 종목 영구 스킵.
6. **회복확인(`LEADER_RECLAIM=true`)**: 종가가 직전 봉 고가를 상향 돌파해야 신호 확정 — **메모리상 유일하게 검증된 robust 개선**([[leader_strategy]], 클린 5일 +12% 3승0패).
7. **앵커 가드**: 현재가가 앵커선 대비 `LEADER_ANCHOR_TOL=0.3%` 이내여야 함.
8. **거래량 필터**(`LEADER_VOLFILTER=0.0` → 라이브에서는 사실상 비활성): 경량 거래량 조건.
9. 장대양봉컷 + 상한가컷(3-1과 공통).

---

## 4단계: 포지션 진입 (`_enter`)

**수량 계산**:
```
slot_budget = LEADER_SLOT_BUDGET_KRW(0.0, 미설정) 이면
              LEADER_BUDGET_KRW(5천만원) / LEADER_MAX_POSITIONS(1)
qty = slot_budget // price
```
현재 라이브는 `LEADER_MAX_POSITIONS=1`, `LEADER_SLOT_BUDGET_KRW=0`이므로 슬롯당 예산 = 전체 예산 5천만원 그대로(포지션 1개만 운용).

진입 시 신호 출처(vwap/pullback)를 `entry_label`/`strategy` 태그로 기록.

`TRADE_DRY_RUN=true`(라이브 실측)이므로 **현재 실제 주문은 나가지 않고 모의(가상) 체결**로 동작 — 위 "메모리 불일치" 섹션 참고.

---

## 5단계: 포지션 관리·청산 (`_manage_position`)

`LEADER_EXIT_MODE=split`(라이브) — 2단계 익절:

```
TP1: 수익률 >= LEADER_SPLIT_TP1_PCT (2.0%)
     → LEADER_SPLIT_TP1_RATIO (50%) 만큼 부분 익절 (_partial_exit)
     → 남은 수량의 손절선을 본전 부근으로 이동
TP2: 수익률 >= LEADER_SPLIT_TP2_PCT (4.0%)
     → 잔량 전량 익절
```

참고로 다른 모드(현재 미사용):
- **trail**: `LEADER_TRAIL_ACTIVATE_PCT(4.0%)` 도달 시 트레일링 스탑 활성화, `trail_stop = peak * (1 - LEADER_TRAIL_GAP_PCT/100(1.5%))`, 단조 비하락(peak 갱신 시에만 상향).
- **기본(고정)**: `LEADER_TP_PCT=4.0%` 고정 익절 목표 / 고정 손절.

**공통 손절**: `LEADER_STOP_BUF_PCT=1.5%` (진입가 대비 완충 손절폭, 신호 기준 stop 대비 버퍼).

**마감청산**: 어느 모드든 `LEADER_CLOSE_TIME=14:55`에 강제 청산(장 마감 전 리스크 정리).

**관전(가상) 모드**: 실매매가 꺼져 있어도(`LEADER_TRADE_ENABLED=false` 또는 dry-run) 동일한 상태머신·차트 스냅샷·신호 판정이 그대로 돌아가며, 실주문 없이 알림+상태갱신만 수행합니다. 현재는 `TRADE_DRY_RUN=true`라 사실상 전량 이 모드로 동작 중.

---

## 6단계: 섹터 회전 (`_maybe_switch` / `_reval_resort`, §4-3 통합 재정렬)

`LEADER_SWITCH_ENABLED=true`(라이브)이면 `LEADER_SWITCH_UNTIL(13:00)`까지 `LEADER_SWITCH_INTERVAL_MIN=5`분마다 재평가:

```
1. 보유 중인 섹터들을 최신 reval.json 기준으로 재점수화
2. 새로 60%룰(sector_score >= top*0.6)을 통과하는 신규 섹터 후보와 합침
3. sector_score 기준 상위 LEADER_MAX_SECTORS(3)개만 유지
4. 밀려난(탈락) 섹터는 삭제하지 않고 "차트전용"으로 강등
   (§4-4: 진입 감시는 중단하되 차트/로그는 계속 표시)
5. 새로 편입된 섹터는 _build_basket으로 바스켓 신규 구성
```

`LEADER_SWITCH_MOVE_MAX_PCT=1.0%` — 전환 트리거로 인정할 최소 가격 변동폭(너무 미세한 변화로는 전환하지 않음, 추정).

---

## 7단계: 디스플레이 점수 (표시 전용, 로직에는 미사용)

웹/알림에 보여주는 100점 만점 점수는 원점수와 별도 변환식을 씁니다. **밴드룰·필터·정렬 판단에는 절대 쓰이지 않고 오직 화면 표시용**입니다.

```
display_score = clamp(raw_score / ceil * 100, 0, LEAD_SCORE_DISP_MAX(100))

종목: ceil = LEAD_SCORE_DISP_STOCK_CEIL  = 0.65
섹터: ceil = LEAD_SCORE_DISP_SECTOR_CEIL = 0.45
```

---

## 부록: 라이브 파라미터 전체 값 (2026-08-16 실측)

### 선별 (`leader_finder.py`)
| 키 | 값 |
|---|---|
| LEADER_SEL_TOP | 100 |
| LEADER_SEL_RISE_MIN | 5 |
| LEADER_SEL_MAX_CHANGE | 25.0 |
| LEADER_SEL_HOT_MIN | 3 |
| LEADER_SEL_VOL_MULT | 2.0 |
| LEADER_SEL_MIN_VALUE_EOK | 500.0 |
| LEADER_SEL_MIN_VALUE_ANCHOR_HHMM | 11:00 |
| LEADER_SEL_MAX_VALUE_EOK | 800.0 |
| LEADER_SEL_MIN_VALUE_FLOOR_EOK | 150.0 |
| LEADER_SEL_MIN_CAP_EOK | 1000.0 |
| LEADER_SEL_TURNOVER_CAP_PCT | 200.0 |
| LEADER_MF_CLAMP_LOW / HIGH | 0.5 / 1.5 |
| LEADER_SEL_SECTOR_TOP3 | true |
| LEADER_DAILY_TREND_GATE | false |
| LEAD_ST_W_VALUE / UPDN / TURNOVER / SURGE | 0.2 / 0.45 / 0.15 / 0.2 |
| LEAD_SC_W_INTENSITY / BREADTH | 0.65 / 0.35 |
| LEAD_SCORE_DISP_STOCK_CEIL / SECTOR_CEIL / MAX | 0.65 / 0.45 / 100.0 |

### 진입·청산 (`leader_trader.py`)
| 키 | 값 |
|---|---|
| LEADER_ENTRY_MODE | **vwap_touch** |
| LEADER_EXIT_MODE | split |
| LEADER_BAND_RATIO | 0.6 |
| LEADER_INTERVAL_MIN | 3 |
| LEADER_W | 2 |
| LEADER_VWAP_TOL | 0.3 |
| LEADER_BAR_RANGE_PCT | 1.5 |
| LEADER_RECLAIM | true |
| LEADER_ANCHOR / TOL / EMA | vwap / 0.3 / 20 |
| LEADER_PHWIN_MIN | 30 |
| LEADER_MAX_PULL_PCT | 5.0 |
| LEADER_FIB_DYNAMIC / FIB_PCT | false / 0.0 |
| LEADER_VOLFILTER | 0.0 |
| LEADER_STOP_BUF_PCT | 1.5 |
| LEADER_TP_PCT | 4.0 |
| LEADER_SPLIT_TP1_PCT / TP1_RATIO / TP2_PCT | 2.0 / 50.0 / 4.0 |
| LEADER_TRAIL_ACTIVATE_PCT / GAP_PCT | 4.0 / 1.5 |
| LEADER_CLOSE_TIME | 14:55 |
| LEADER_OWN_SYMBOL_PRIORITY | true |
| LEADER_MAX_SECTORS | 3 |
| LEADER_SWITCH_ENABLED / INTERVAL_MIN / UNTIL / MOVE_MAX_PCT | true / 5 / 13:00 / 1.0 |
| LEADER_TRADE_ENABLED | true |
| LEADER_BUDGET_KRW | 50,000,000 |
| LEADER_MAX_POSITIONS | 1 |
| LEADER_SLOT_BUDGET_KRW | 0.0 (→ 예산/포지션수로 자동) |
| **TRADE_DRY_RUN** | **true** (모의, 실주문 없음) |
