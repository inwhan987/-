# 스윙봇 작업 지시 — 축별 점수 기록 + DART 정리

대상 저장소: `stock-bot/`
선행 문서: `SWING_BOT_DESIGN.md` (먼저 읽을 것)

이번 작업의 목적은 **기록**이다. 진입/청산 판단은 하나도 바꾸지 않는다.
지금 로그는 `score` / `pscore` 두 개뿐이라 나중에 결과를 봐도
"타점이 좋아서 번 건지, 재무가 좋아서 번 건지"를 구분할 수 없다.
축을 나눠서 남겨두면 나중에 가중치를 사후에 실험할 수 있다.

---

## 0. 금지사항 (어기면 결과가 무의미해진다)

- `screener.py`, `leader_finder.py`, `stock_bot/live/**`, `stock_bot/broker/kis_ws.py`
  **수정 금지**. 대장주봇이 실전 자금으로 돌고 있다.
- `bt_swing/strategies.py` 의 **전략 조건·점수 산식 변경 금지**.
  이번엔 축 기록만 추가한다. 조건을 건드리면 기존 3.7년 결과와 대조가 안 된다.
- **진입 게이트 기준을 임의로 낮추지 말 것.** `--no-fund`, `--no-cap`,
  `block_when_cap_missing=False` 같은 우회 금지. 데이터가 없으면 NULL 로 두고
  건너뛰지, 게이트를 끄지 않는다.
- 점수 축이 비었을 때 **0 을 넣지 말 것.** 반드시 `NULL`.
  0 은 "나쁘다", NULL 은 "모른다" — 이게 섞이면 나중에 분석이 전부 틀어진다.
- `.env` 는 열지 말 것. `DART_API_KEY` 는 환경변수로만 읽고 로그·DB에 절대 남기지 않는다.

---

## 1. DART 를 주간 전종목 배치로 분리

### 왜 바꾸나
지금 `scripts/swing_nightly.py` 는 이렇게 돼 있다.

```python
sig, wl = daily_scan.run_nightly_scan(date)
...
n = dart.collect(sorted(set(wl["code"])), date, budget_sec=c.dart_budget_sec)
```

두 가지가 잘못됐다.

1. **감시 리스트 30종목만 받는다** → 백분위를 낼 모집단이 30개뿐이라
   다른 날과 비교가 안 된다. 백분위는 그날 유니버스 전체 대비여야 한다.
2. **스캔 뒤에 실행된다** → 그날 신호에 재무 점수를 붙일 수가 없다.
   이미 `signals` 를 다 쓴 뒤다.

분기 재무는 하루 단위로 변하지 않는다. 그러니 야간 배치에서 빼고
주 1회 전종목 배치로 미리 쌓아둔 뒤, 야간 스캔은 **읽기만** 한다.

### 1-1. `dart_fin` 에 `rcept_dt` 추가

현재 스키마는 `PRIMARY KEY (date, code)` 이고 `date` 는 *수집일*이다.
"언제 공시됐는지"가 없어서 **과거 백필이 불가능**하다.
백테스트로 재무 축을 검증하려면 접수일이 필요하다.

- `fnlttSinglAcnt.json` 응답 행에 `rcept_no` 가 있다. 앞 8자리가 접수일자(YYYYMMDD).
  실제 응답을 찍어서 **필드가 있는지 먼저 확인**하고, 없으면 `list.json`
  (공시목록 API)로 보완할지 판단해서 보고할 것. 추측으로 넣지 말 것.
- 컬럼 추가 (기존 DB 가 있으므로 `ALTER TABLE` + 존재 여부 체크로 멱등하게):

```sql
ALTER TABLE dart_fin ADD COLUMN rcept_dt TEXT;   -- 공시 접수일 YYYYMMDD
ALTER TABLE dart_fin ADD COLUMN fiscal   TEXT;   -- 2025Q3 / 2024A
```

- `annual_year` + `qtr_label` 로 `fiscal` 을 채운다.
- **점수를 붙일 때 쓰는 날짜는 `rcept_dt` 다.** `fiscal` 기준으로 붙이면
  결산일과 공시일 사이 약 6주만큼 미래참조가 된다.

### 1-2. `scripts/swing_dart_weekly.py` 신규

```
python scripts/swing_dart_weekly.py [--budget-sec 3600] [--force]
```

- 대상: `store.all_codes()` 전체 (감시 리스트 아님)
- `dart.collect()` 를 그대로 재사용. 캐시 TTL 30일이 이미 있어서
  2회차부터는 API 호출이 거의 없다.
- DART 일일 호출 한도(status `020`)에 걸리면 **즉시 중단하고 기록**.
  이미 `DartError` 로 올라오게 돼 있으니 그 경로 유지.
- `store.mark_run(date, "dart_weekly", ...)` 로 결과 기록.
- 첫 실행은 전종목이라 오래 걸린다. `--budget-sec` 안에서 끊고
  다음 실행이 이어받도록 (캐시가 있으니 자연히 이어받는다).

### 1-3. `swing_nightly.py` 에서 DART 호출 제거

`c.dart_enabled` 분기 블록을 통째로 뺀다. 야간 배치는 `dart_fin` 을 읽기만 한다.
`.env.swing` 의 `SWING_DART_ENABLED` 는 **주간 배치 on/off 스위치**로 의미를 바꾸고
주석도 같이 고칠 것. `SWING_DART_BUDGET_SEC` 는 그대로 둔다.

---

## 2. `signals` 축별 점수 컬럼

### 2-1. 스키마

```sql
ALTER TABLE signals ADD COLUMN setup_score   REAL;  -- 셋업(타점) 원점수 = 기존 score
ALTER TABLE signals ADD COLUMN setup_pscore  REAL;  -- 전략 내 백분위 0~100 = 기존 pscore
ALTER TABLE signals ADD COLUMN value_score   REAL;  -- PER/PBR      (fund)
ALTER TABLE signals ADD COLUMN quality_score REAL;  -- ROE·부채비율  (dart_fin)
ALTER TABLE signals ADD COLUMN growth_score  REAL;  -- 매출·순이익 YoY (dart_fin)
ALTER TABLE signals ADD COLUMN flow_score    REAL;  -- 외인·기관 수급 (flow)
ALTER TABLE signals ADD COLUMN liq_score     REAL;  -- 거래대금·시총  (daily/meta)
ALTER TABLE signals ADD COLUMN prog_score    REAL;  -- 프로그램매매   (program)
ALTER TABLE signals ADD COLUMN total_score   REAL;  -- 참고용 단순 평균
ALTER TABLE signals ADD COLUMN rank_basis    TEXT;  -- 실제 순위에 쓴 기준
```

`score`/`pscore` 는 **지우지 말 것**. 기존 행과의 연속성이 끊긴다.
`setup_score`/`setup_pscore` 에 같은 값을 중복으로 넣는다.

### 2-2. `daily_scan.scan_panel()` 에 재료 조인

지금 신호 행에는 가격·지표·거래대금·시총만 담긴다.
`flow` / `fund` / `program` / `dart_fin` 테이블은 이미 있는데 조인을 안 한다.
스캔 대상 날짜 기준으로 아래를 붙인다.

| 축 | 재료 | 어느 날짜를 쓰나 |
|---|---|---|
| `value` | `fund.per`, `fund.pbr` | 스캔일(당일 종가 기준 공표치) |
| `flow` | `flow.forgn`, `flow.inst` 최근 5·20일 누적 | 스캔일까지 (전일 확정치 포함) |
| `prog` | `program.ntby_value` 최근 5일 누적 | 스캔일까지 |
| `liq` | `value_ma20`, `mktcap_eok` | 스캔일 |
| `quality` | `dart_fin.returnOnEquity`, `debtToEquity` | **`rcept_dt <= 스캔일` 중 최신** |
| `growth` | `dart_fin.qtr_rev_growth`, `qtr_inc_growth` | **`rcept_dt <= 스캔일` 중 최신** |

DART 두 축의 날짜 조건이 핵심이다. `rcept_dt` 가 스캔일보다 뒤인 행을
쓰면 그건 미래참조다. 쿼리에 반드시 조건으로 넣을 것.

### 2-3. `add_ranks()` 에서 백분위 계산

- **모집단은 그날 신호가 난 종목 전체** (게이트 통과분). 전략별이 아니다.
  `setup_pscore` 만 전략 내 백분위 — 전략마다 원점수 스케일이 달라서다.
- 방향 정리 (IC 실측 기준, 높을수록 좋게 맞춘다):
  - `value`: **저PER·저PBR 이 유리** (IC per −18.1, pbr −22.0) → 낮을수록 높은 점수
  - `quality`: ROE 높을수록 ↑, 부채비율 낮을수록 ↑
  - `growth`: YoY 높을수록 ↑
  - `flow`: 순매수 누적 클수록 ↑
  - `liq`: 거래대금 클수록 ↑
  - `prog`: 순매수 클수록 ↑ (검증 전이라 기록만)
- 두 재료를 합치는 축(`value`, `quality`)은 **각각 백분위 낸 뒤 평균**.
  원값을 먼저 더하면 단위가 달라서 한쪽이 먹힌다.
- 재료가 없으면 그 축은 `NULL`. 두 재료 중 하나만 있으면 있는 쪽만 쓴다.
- `total_score` = 값이 있는 축들의 단순 평균 (참고용, 판단에 쓰지 않음).

### 2-4. 진입 판단은 그대로

`build_watchlist()` 는 **지금처럼 `setup_pscore` 순위로만** 고른다.
다른 축은 기록만 한다. `rank_basis` 에 `"setup_pscore"` 라고 적어둔다.
나중에 기준을 바꾸면 이 값이 바뀌므로 어느 날부터 뭐가 달라졌는지 추적된다.

이유: 지금 다른 축을 순위에 섞으면, 나중에 결과를 봐도
"내가 넣은 가중치 때문에 이렇게 된 것"과 "축이 실제로 효과가 있는 것"을
구분할 수 없다. 축은 깨끗하게 남겨둬야 사후에 비교가 된다.

---

## 3. `bt_swing` 에도 같은 축 기록

이미 받아둔 2022-01 ~ 2025-08 / 2,483종목 데이터가 있으니,
축을 기록하게만 해두면 **재수집 없이 바로 조합 분석**을 돌릴 수 있다.

- `bt_swing/report.py` 의 `selection_report()` 가 뱉는 신호 테이블에
  위 8개 축 컬럼을 같은 정의·같은 방향으로 추가.
- DART 는 과거 `rcept_dt` 가 없으면 백필이 안 된다.
  **안 되면 `quality`/`growth` 는 NULL 로 두고, 된다/안 된다를 보고할 것.**
  억지로 `fiscal` 날짜를 붙이지 말 것 (6주 미래참조).
- 축별로 Q1~Q5 나눠서 20일 수익률 표를 출력.
  → 어느 축이 실제로 변별력이 있는지 바로 보인다.

---

## 4. 남은 확인 두 개 (같이 처리)

### 4-1. `test_kis_data.py` 실행 결과 보고
파일은 있는데 결과를 모른다. 실행해서 아래를 표로 정리할 것.

- 수급(투자자별)·프로그램매매·PER/PBR 을 KIS 에서 **과거 일별**로 받을 수 있나
- 당일치만 주면 매일 저녁 1회씩 쌓아야 한다 → 초기 60영업일 이력은 pykrx 백필
- pykrx 를 쓸 경우 **`--rps 0.7` 고정**. 전에 IP 1일 차단당한 이력이 있다.

### 4-2. `daily_scan.scan()` ↔ `backtest_swing_daily.py` 신호 수 대조
같은 날짜·같은 전략·같은 `use_trend` 로 돌렸을 때 신호 종목 수가 일치해야 한다.
불일치하면 라이브와 백테스트가 다른 걸 보고 있다는 뜻이라 **여기서 멈추고 보고**.
대조 날짜는 최근 5영업일.

---

## 5. 완료 조건

1. `python scripts/swing_dart_weekly.py --budget-sec 600` 이 돌고
   `dart_fin` 에 `rcept_dt` 가 채워진 행이 생긴다
2. `python scripts/swing_nightly.py --date <최근영업일> --no-collect` 실행 후
   `signals` 에 축 컬럼이 채워진다 (없는 축은 NULL, 0 아님)
3. 축별 채움률을 출력: `setup 100% / value xx% / quality xx% / ...`
   **채움률이 낮은 축은 그대로 보고할 것.** 억지로 채우지 말 것.
4. `build_watchlist` 결과가 축 추가 **전후로 동일**함을 확인
   (진입 판단을 안 바꿨으므로 같아야 한다. 다르면 버그다)
5. 4-1, 4-2 결과 보고

작업 후 변경 파일 목록과 위 5개 항목 결과를 표로 정리해서 보고할 것.
