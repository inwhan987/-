# 대장주봇 — 선별 이후 재선별·재정렬 동작

작성 2026-08-20 · 기준 커밋 `3d731cd`

정본 선별(09:30/09:40)이 끝난 뒤 장중에 무슨 일이 벌어지는지를 코드 기준으로
정리한 문서. 용어부터 하나 정리하고 시작한다.

> **"순위계산"과 "재선별"은 별개 동작이 아니다.**
> 서로 다른 두 프로세스가 하는 두 단계이고, 트레이더 쪽에 "순위만 다시
> 계산하는" 경로는 존재하지 않는다.

| | 1단계 · 재선별 | 2단계 · 재정렬 |
|---|---|---|
| 주체 | `leader_finder.py` 서브프로세스 | `leader_trader.py` (1분 tick) |
| 주기 | `LEADER_SWITCH_INTERVAL_MIN` = 5분 | 매 1분, 스탬프 변경 시에만 실질 동작 |
| 범위 | 전체 시장 처음부터 | 보유 섹터 + 밴드 통과 신규 |
| 기존 상태 참조 | **안 함** | 함 (보유 섹터를 후보에 강제 포함) |
| 산출물 | `data/leader_picks/{date}_reval.json` | 트레이더 감시 바스켓 + 상태파일 |

---

## 0. 정본 선별 (09:30 / 09:40)

- cron 이 `leader_finder.py` 를 돌려 `data/leader_picks/{date}.json` 생성.
- 트레이더가 `_load_day()` 에서 이 파일을 읽어 초기 감시 바스켓을 만든다.
- 첫 로드 시 **다중 섹터 시딩**: 1등 `sector_score` 대비
  `LEADER_BAND_RATIO`(=0.6) 이상인 섹터를 `LEADER_MAX_SECTORS`(=3)개까지
  함께 감시로 잡는다.
- 각 섹터 안에서는 `_build_basket()` 이 `stock_score ≥ 1등 × 0.6` 인 종목만
  남긴다 (최대 top3).

---

## 1단계 · 재선별 — `leader_finder` 서브프로세스

`leader_runner.py` 가 `LEADER_SWITCH_INTERVAL_MIN`(5분)마다,
`LEADER_SWITCH_UNTIL`(13:00)까지 서브프로세스를 띄운다.

```
leader_finder.py --once --theme --summary-only --reval <정본과 동일 인자>
```

- **완전히 새로 선별한다.** 아침 정본과 같은 인자로 전체 시장을 처음부터 다시
  훑는다. 현재 트레이더가 뭘 감시 중인지 **전혀 참조하지 않는다.**
- 결과를 `{date}_reval.json` 에 **덮어쓴다.** 그 시점 기준 상위 섹터들과
  각 섹터의 top3 종목·점수(`sector_score`, `stock_score`)가 들어간다.
- 이 단계는 트레이더 상태를 건드리지 않는다. **파일만 쓴다.**

### 첫 재선별 타이밍 (2026-08-20 수정, `5e88175`)

기동 후 `_last_reval["t"]` 가 `None` 이면 interval 게이트를 그냥 통과해,
정본 선별 직후 90초 만에 같은 결과를 다시 뽑는 일이 있었다.
지금은 정본 파일의 mtime 을 "마지막 재선별"로 간주해 interval 만큼 기다린다.

---

## 2단계 · 재정렬 — `leader_trader`

`_maybe_switch()` → `_reval_resort()` ([leader_trader.py:399](../stock_bot/live/leader_trader.py))

### 게이트 3개 — 하나라도 걸리면 아무 일도 안 일어난다

1. **슬롯 여유** — `len(positions) < LEADER_MAX_POSITIONS`.
   슬롯을 다 쓰면 tick 이 `return` 해서 `_maybe_switch` 가 호출조차 안 된다.
2. **시각** — 현재시각 ≤ `LEADER_SWITCH_UNTIL`(13:00).
3. **스탬프** — `reval.json` 의 `selected_at` 이 직전 처리분과 달라야 한다.
   (자체 타이머가 아니라 파일 스탬프로 게이트 — 재선별 로그와 재정렬 로그의
   시각이 어긋나 보이던 혼동을 없애기 위해 바꿈. 중복 처리 없음.)

### 섹터 레벨 — 통합 재정렬

```
combined = { 보유 중인 섹터 전부 : reval 최신 점수 (reval 에 없으면 0점) }
         ∪ { 신규 섹터 : sector_score ≥ reval 1등 점수 × 0.6 인 것만 }

ranked = combined 를 점수 내림차순 정렬
keep   = ranked[:LEADER_MAX_SECTORS]      # 상위 3개
```

- `keep` 안의 **보유 섹터** → 그대로 유지
- `keep` 밖으로 밀린 **보유 섹터** → **삭제가 아니라 차트전용**
  (`_chart_only_sectors`) 으로 이동. 진입 스캔만 중단하고 차트는 계속 그린다.
  점수가 다시 오르면 승격 가능.
- `keep` 안의 **미보유 섹터** → 새로 바스켓 생성
- `keep` 밖의 신규 후보 → 로그로 사유만 남김
  (`밴드미달` / `슬롯초과` — "재선별엔 나오는데 왜 추가가 안 되냐"는 착시 방지)

> **퇴출이 일어나는 조건**
> `combined` 의 총 후보 수가 `LEADER_MAX_SECTORS` 를 넘을 때만이다.
> 2섹터를 들고 있고 밴드를 통과한 신규가 없으면, 보유 섹터 점수가 0점이어도
> 3자리 안에 들어 그대로 유지된다.

### 종목 레벨 — 편입 시점 고정

```python
for s in keep:
    if s in self._sector_baskets:
        continue                                   # ← 종목을 손대지 않음
    nb = self._build_basket(self._top3_of(L))      # 새 섹터만 60%룰로 생성
```

**이미 감시 중인 섹터 안에서는 종목 순위 재계산도, 종목 추가·교체도 일어나지
않는다.** 편입될 때 그 시점 점수로 60%룰을 돌려 나온 종목들이 장 끝까지 그대로다.
reval 에서 그 섹터의 2등이 1등으로 바뀌어도 트레이더 바스켓은 안 바뀐다.

→ **종목이 추가되는 유일한 경로는 "새 섹터가 통째로 편입될 때"** 뿐이다.

### 진입 스캔 우선순위 재정렬 (2026-08-20 추가, `47f01ca`)

```python
self._sector_baskets = {s: self._sector_baskets[s] for s in keep if s in ...}
```

`_flatten_baskets()` 가 dict 삽입 순서를 그대로 쓰기 때문에, 여기서 `keep`
(=점수순)으로 다시 깔아두지 않으면 스캔 우선순위가 **점수순이 아니라 편입
시간순**으로 굳는다. 동시에 신호가 뜨면 편입이 빨랐던 낮은 점수 섹터가 슬롯을
먼저 먹는다.

### 상태 저장

`after == before`(감시 섹터 집합 변화 없음)면 `last_switch_eval` 만 갱신하고
끝낸다. 변화가 있을 때만 `active_sector_name`, `watched_sectors`,
`sector_starts`, `chart_only_sectors` 를 갱신하고 `🔄 섹터 재정렬` 로그 +
디스코드 알림을 낸다.

저장 위치는 `data/leader_trade_state/{date}.json`.
**주의: `_sector_baskets`(종목 목록)는 저장되지 않는다** — 섹터 이름만 저장된다.

---

## 알려진 불일치 — 웹 대시보드 vs 트레이더

| | 종목 구성 결정 방식 |
|---|---|
| 트레이더 | 편입 시점 60%룰 결과를 **고정** (위 참조) |
| 웹 | 매 요청마다 **최신 reval.json 으로 60%룰 재계산** ([services.py:448](../stock_bot/web/services.py)) |

그래서 장중에 2등 종목 점수가 1등의 60% 밑으로 떨어지면 웹에선 그 종목이
사라지지만 트레이더는 여전히 감시·진입 대상으로 들고 있다. 화면이 실제 매매
대상과 어긋난다.

또 하나: 트레이더는 **재시작 시 바스켓을 상태파일에서 복원하지 않고 최신
picks 로 `_build_basket()` 을 다시 돌린다**. 즉 "편입 시점 고정"은 프로세스가
살아있는 동안만 유효하고, 컨테이너가 한 번 재시작되면 웹과 같은 방식으로
재계산된다.

### 해결 후보 (미결정)

1. **얕은 버전** — 트레이더가 `sector_baskets` 를 상태에 *저장만* 하고 복원
   정책은 그대로. 웹은 그걸 **구성(멤버십)에만** 쓰고 등락률·점수는 계속
   picks 에서 조인. 없으면 현행 재계산으로 폴백.
   → 매매 로직 무변경, 화면만 정합.
2. **완전 통일** — 재시작 복원까지 저장된 바스켓 사용.
   → 재시작 후 진입 대상 종목이 지금과 달라진다. **매매 로직 변경**이라 승인 필요.

---

## 참고 · 관련 파라미터

| 키 | 현재값 | 역할 |
|---|---|---|
| `LEADER_SWITCH_ENABLED` | on | 재정렬 자체 토글 |
| `LEADER_SWITCH_INTERVAL_MIN` | 5 | 재선별 서브프로세스 주기(분) |
| `LEADER_SWITCH_UNTIL` | 13:00 | 재선별·재정렬 종료 시각 |
| `LEADER_MAX_SECTORS` | 3 | 동시 감시 섹터 상한 |
| `LEADER_BAND_RATIO` | 0.6 | 섹터·종목 양쪽 60%룰 |
| `LEADER_MAX_POSITIONS` | — | 슬롯 수. 소진 시 재정렬 정지 |

모두 `_HOT_FIELDS` + `_LEADER_KEYS` 에 등록되어 있어 웹에서 고치면 재시작 없이
반영된다(2026-08-20 전수 확인).
