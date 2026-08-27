# 네이버 크롤링 전수 지도

이 레포가 네이버에서 긁어오는 **모든** 데이터의 목록이다. 무엇을 · 어디서 ·
누가 · 얼마나 자주 · 실패하면 어떻게 되는지를 한 장에 모은다.

네이버를 쓰는 이유는 하나로 요약된다: **KIS 유량 한도(모의 1건/초·실전 18건/초)와
완전히 분리된 채, 인증 없이, 순위·테마·업종·지수 일봉처럼 KIS가 안 주거나
비싸게 주는 것을 준다.** 대신 스크래핑이라 언제든 마크업이 바뀔 수 있어,
**전부 "실패해도 예외를 올리지 않고 빈 값을 돌려준다"** 는 규칙을 지킨다.

---

## 0. 한눈에 보기

| # | 데이터 | 엔드포인트 | 모듈 | 호출 주기 | 실패 시 |
|---|--------|-----------|------|----------|---------|
| 1 | 핫테마 목록(등락률) | `sise/theme.naver` | `leader_finder.fetch_theme_list` | 선별 tick 마다 | 선별 중단(핫테마 0) |
| 2 | 테마 구성종목 | `sise/sise_group_detail.naver?type=theme` | `leader_finder.fetch_theme_stocks` | 09:05 프리페치 1회 + miss 시 | 그 테마만 누락 |
| 3 | 업종(섹터) | `item/coinfo.naver` → `sise_group_detail?type=upjong` | `leader_finder.sector_of` / `screener._naver_industry` | 영구 캐시 miss 시 | `"(미상)"` |
| 4 | 거래대금 순위 KRX | `sise/sise_quant.naver` | `naver_quant._fetch_naver_quant` | 선별·reval tick | KIS 폴백 |
| 5 | 거래대금 순위 NXT | `sise/nxt_sise_quant.naver` | 〃 (`nxt=True`) | 〃 | KRX 값만 |
| 6 | 지수 일봉(레짐) | `fchart.stock.naver.com/sise.nhn` | `broker/naver_index.py` | 시장당 하루 1회 | 게이트 통과(차단 안 함) |
| 7 | 전일 분봉(종가) | 〃 `timeframe=minute` | `broker/naver_minute.py` | 워밍업 시 | `[]` (당일봉만) |
| 8 | 실시간 시세 | `polling.finance.naver.com/api/realtime` | `broker/naver_quote.py` | 대시보드 폴링 | 직전값 유지 |
| 9 | 종목 뉴스 | `item/news_news.naver` | `news/crawler.py` | `NEWS_CRAWL_INTERVAL_MINUTES` | 그 종목 스킵 |
| 10 | 뉴스 검색 API | `openapi.naver.com/v1/search/news.json` | `news/naver_api.py` | 백필 수동 | — |
| 11 | 종목명 | `item/main.naver` | `names.py` | 캐시 miss 시 | 코드 그대로 |
| 12 | 종목 검색(자동완성) | `m.stock.naver.com/front-api/search/autoComplete` | `names.search_stocks` | 웹 입력 시 | 빈 목록 |
| 13 | 상장시장(KOSPI/KOSDAQ) | `m.stock.naver.com/api/stock/{code}/basic` | `naver_index.stock_market` | 프로세스 수명 캐시 | `"KOSPI"` 가정 |
| 14 | EPS·PER 컨센서스 | `navercomp.wisereport.co.kr/company/cF1002.aspx` | `screener.py` | 스크리너 실행 시 | 빈 dict |

**유일하게 인증이 필요한 건 #10(개발자 API, `NAVER_CLIENT_ID/SECRET`)** 이다.
나머지 13개는 전부 비인증 스크래핑이다.

---

## 1. 핫테마 목록 — `fetch_theme_list()`

```
GET https://finance.naver.com/sise/theme.naver?&page=N
```

대장주 봇의 **출발점**이다. "오늘 어느 테마가 달리고 있나"를 여기서 받는다.

- 1페이지를 먼저 받아 `page=(\d+)` 최대값으로 총 페이지 수를 알아낸 뒤 전 페이지 순회
  (통상 263개 안팎).
- 테마번호·이름은 정규식 `type=theme&no=(\d+)[^>]*>([^<]+)</a>`, 등락률은
  `pd.read_html` 로 테이블을 떠서 **두 번째 컬럼**.
- 반환: `[{"no": "505", "name": "로봇", "change_pct": 6.83}, ...]` — 등락률 내림차순.

### 함정 두 개 (둘 다 실제로 물렸다)

**① `read_html` 의 NaN 간격행.** 네이버 테마 테이블에는 테마 사이에 빈 행이 섞여
있어 테이블 행 수 > 정규식으로 뽑은 테마명 수가 된다. 인덱스를 그대로 맞추면
MLCC 등 **약 100개 테마의 등락률이 NaN으로 밀려 통째로 누락**됐다. 그래서
`chg_vals.dropna().reset_index(drop=True)` 로 재색인해야 테마명 i ↔ 등락률 i 가 맞는다.

**② `min_change` 는 기본 -100(사실상 끔).** 테마 전체가 하락이어도 그 안에 급등
종목이 숨어 있기 때문이다(예: '반도체 장비' 테마등락 -2.5%인데 +3%↑ 종목 6개).
핫테마 판정은 테마 등락률이 아니라 **구성 종목의 상승률(`rise_min`·`hot_min`)** 로 한다.

### 캐시하지 않는다

등락률은 실시간 값이라 캐시하면 의미가 없다. 테마 **목록**은 매 선별 tick 마다
새로 긁는다. 캐시하는 건 다음 항목(구성종목)뿐이다.

---

## 2. 테마 구성종목 — `fetch_theme_stocks()` / `prefetch_themes()`

```
GET https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={NO}
```

정규식 `code=(\d{6})` 로 종목코드 집합만 뽑는다.

### 09:05 프리페치가 존재하는 이유

러너는 선별 tick 마다 `leader_finder.py` 를 **새 서브프로세스**로 띄운다. 모듈 전역
`_THEME_STOCK_CACHE` 가 매 회차 비어 있어서, 263개 테마 상세 페이지를 **매번**
재크롤링했다(실측 80~120초). 09:28:30에 시작해도 종료가 09:31을 넘고, 미선별
재시도 회차마다 같은 크롤을 반복했다.

→ `leader_runner.py:216 _leader_prefetch_themes` 가 평일 **09:05**에
`leader_finder.py --prefetch-themes` 를 돌려 `data/leader_theme_cache.json`
(날짜 키)에 적재한다. 선별(09:30)까지 25분 여유.

**왜 08:30이 아니라 09:05인가**: 테마 편입/제외가 개장 무렵 반영될 수 있어
장전 스냅샷은 그날 구성과 어긋날 수 있다.

**misfire_grace_time=1800**: 기본 grace 1초로는 09:05 직전 재배포 시 부팅 백필이
메인스레드를 잡고 있으면 `scheduler.start()` 가 09:05:01로 밀려 통째로 미스파이어된다
(실측 2026-08-20: 09:03 배포 → 프리페치 스킵 → 픽이 263테마 직접 크롤 47.6초).

### 빈 결과는 캐시하지 않는다

165개 테마를 연속 크롤링하다 한 페이지가 실패하면 빈 집합이 캐시돼 **그 테마가
해당 회차 핫섹터 판정에서 통째로 누락**된다. 그래서 1회 재시도(1초 간격) 후에도
비면 캐시에 넣지 않는다.

### 밸류업 테마 제외

`THEME_EXCLUDE = ("밸류업", "value-up", "value up")` — 정부 정책 묶음은 대형 상승주를
거의 다 포함해 진짜 섹터(반도체 장비 등)를 가린다.

---

## 3. 업종(섹터) — 2단 크롤

```
GET https://finance.naver.com/item/coinfo.naver?code={CODE}     → upjong&no=(\d+)
GET https://finance.naver.com/sise/sise_group_detail.naver?type=upjong&no={NO}
                                                                 → <title> 에서 업종명
```

종목 하나당 **요청 2건**이다. 유니버스 200종목이면 400건. 이게 초기에 매 선별마다
70초를 먹었다.

→ `data/leader_sector_cache.json` 에 **날짜 키 없이 영구 캐시**. 종목의 업종은
상장 기간 내내 사실상 불변이기 때문이다.

**단, `"(미상)"` 은 저장하지 않는다.** 날짜 만료가 없는 캐시라 일시적 네트워크
실패값을 굳히면 영구 오염된다.

`screener.py` 에도 같은 2단 크롤이 `_naver_industry()` 로 따로 있다(스크리너는
별도 프로세스라 캐시를 공유하지 않는다).

---

## 4~5. 거래대금 순위 — KRX + NXT 통합

```
GET https://finance.naver.com/sise/sise_quant.naver?sosok={0|1}       # KRX 정규시장
GET https://finance.naver.com/sise/nxt_sise_quant.naver?sosok={0|1}   # 넥스트레이드
```

`sosok`: 0=코스피, 1=코스닥. **`page` 파라미터는 네이버가 무시**하므로 시장당 ~100종목
고정이다. 인코딩은 **euc-kr**.

파싱은 2단계다. 종목 행 앵커 정규식으로 `code`↔`name` 순서를 보존해 매핑을 만들고,
`pd.read_html` 로 `N`·`종목명`·`거래대금` 컬럼이 있는 테이블을 골라 수치를 붙인다.

### NXT를 왜 합치나 — 로봇섹터가 사라졌던 사건

같은 종목의 거래대금은 KRX/NXT로 **분리 집계**된다(검증: 삼성전자 KRX 6.6조 +
NXT 6.1조). KRX 단독 순위만 보면 NXT 거래대금(고가주는 KRX의 ~90% 규모)을 놓쳐
대형주가 과소계상되고, 그 결과 섹터가 통째로 순위에서 밀려난다.

`_merge_krx_nxt()` 가 종목코드 기준으로 합산한다:
- `value_won`, `volume` → **합산**
- 가격·등락률·시총 → **KRX 값 우선**, KRX에 없는 종목만 NXT 값

한쪽 페이지에만 든 종목은 그 한쪽 값만 반영된다(반대 거래소분 미관측) — KIS의
거래소별 30행 컷과 같은 경계 한계지만, KRX 단독보다 통합값에 훨씬 근접한다.

### 시장별로 top_n 을 따로 자른다

코스피+코스닥을 합산한 뒤 자르면 코스닥 종목이 코스피 대형주에 밀려 상위 N에서
탈락한다. 그래서 `fetch_ranking` / `fetch_ranking_unified` 는 **시장마다** top_n 씩
가져와 합친다.

### 보통주 필터

`_is_common_stock()` = 코드 끝자리 0(우선주 제외) + ETF/ETN 이름 제외
(`_ETF_PREFIXES`, `_ETF_BRANDS_SPACED`, `_FUND_CODES`).

### 소스 우선순위 (leader_finder.py:62~72)

| 용도 | 1순위 | 2순위 |
|------|-------|-------|
| 거래대금 순위 | **네이버 KRX+NXT 통합** `fetch_ranking_unified` | KIS 통합 `kis_quant.fetch_ranking` |
| 유니버스 | 다음 `daum_quant` / 매경 `mk_quant` (KRX 단독) | KIS KRX |
| 시가총액 | 매경 `mk_quant.fetch_marketcap_map` (장 시작 전 1회) | — |

네이버가 1순위인 이유: 3분 주기 reval이 도입되면서 KIS UN 재조회의 α건 부담이
stock-bot 체결 지연을 유발할 위험이 생겼다. **실시간성은 스크래핑 시차(수초)를
감수하고, 유량 무제한을 택했다.**

> `naver_quant.py` 는 **부작용 없는 순수 모듈**이어야 한다 — 모듈 레벨에서 stdout을
> 재설정하지 말 것. `market_analysis --json` 모드가 stdout redirect 상태에서 import 한다.

---

## 6. 지수 일봉 — 베어장 게이트 (`broker/naver_index.py`)

```
GET https://fchart.stock.naver.com/sise.nhn?symbol={KOSPI|KOSDAQ}&timeframe=day&count=120
```

**KIS는 일봉 지수 히스토리를 주지 않는다.** 그래서 fchart를 쓴다. 종가만 사용.

앙상블은 평균회귀(눌림목 매수)라, 지속 하락장(예: 코스닥 -20%)에서 계속 진입했다가
손절당해 출혈한다. 그래서 **종목이 속한 시장지수**의 레짐이 '베어'면 신규 매수를 차단한다.

베어 판정은 **AND** 다:
- 종가 < `REGIME_MA_PERIOD`일 이동평균 (추세선 아래)
- `REGIME_MOM_DAYS`일 모멘텀 < 0 (하락 지속)

AND인 이유: 단발 폭락(MA 위)은 통과시켜 눌림목 매수를 살리기 위해서다.
2026-07-02에 MA 50→20으로 하향(50MA는 반응이 느려 급락장 차단이 늦음).

- 시장당 **하루 1회** 호출 후 KST 날짜로 캐싱 → 외부호출 최소.
- 실패/데이터부족 시 **False(=통과)** 반환. 데이터가 없다고 매매를 막지 않는다.

종목의 상장시장은 `m.stock.naver.com/api/stock/{code}/basic` 으로 1회 조회 후
프로세스 수명 캐시(상장시장은 사실상 불변).

---

## 7. 전일 분봉 — 지표 워밍업 (`broker/naver_minute.py`)

```
GET https://fchart.stock.naver.com/sise.nhn?symbol={CODE}&timeframe=minute&count=3000
```

KIS 당일분봉 TR(`FHKST03010200`)은 오늘 것만 준다. 장초반(9:40)에 RSI/볼린저/MACD/
EMA120 같은 종가 기반 지표를 데우려면 전일 봉이 필요한데, **모의 서버는 과거 분봉
TR(`FHKST03010230`)을 막아둔다.**

fchart 분봉은 약 **6거래일치 1분봉**을 주지만 **종가만 유효**하다(O/H/L은 null,
거래량은 누적).

두 가지 용도:
1. `fetch_prev_closes` — 종가 시리즈 워밍업.
2. `fetch_prev_ohlcv` — 1분 종가 5개를 묶어 N분봉 **유사 OHLC** 합성
   (o=첫 종가, h=max, l=min, c=막 종가) → ST/PSAR/HTF-ADX 워밍업.
   실제 고저보다 폭이 약간 좁다(1분 내 극값 누락). 2026-07-15 검증: 실 5분봉 대비
   ST(7,3) 방향 일치율 **95.6~100%** (당일봉만 쓰던 기존 67~88%).

**VWAP·ATR(손절)은 여전히 당일 실봉만 쓴다 — 여기 데이터를 넣지 않는다.**

원칙: 어제 종가는 **부족분(deficit)만** 앞에 붙인다. 9:40 5분봉 기준 오늘 봉이 8개면
20봉 필요한 지표는 어제서 12개만 빌린다. 실패해도 `[]` 를 돌려 라이브를 멈추지 않는다.

---

## 8. 실시간 시세 — 대시보드 표시 전용 (`broker/naver_quote.py`)

```
GET https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:005930,000660,...
```

KIS `inquire-price` 는 모의 1건/초 한도를 웹·스톡봇·대장주봇 **3프로세스가 파일락으로
공유**한다. 종목 직렬 조회 + 간헐 재시도가 프론트 8초 타임아웃을 넘겨 대시보드에
'갱신 지연' 배지가 뜨곤 했다.

네이버 폴링은 인증 불요 · KIS 유량과 완전 분리 · `delayTime=0`(실시간) ·
**한 번에 여러 종목**(응답 수십 ms).

> **매매 판단엔 절대 쓰지 않는다. 화면 표시 전용이다.**
> 실패 시 빈 dict → 호출측이 직전값 유지.

`rf`(등락 구분) 코드: 1 상한·2 상승 → `+`, 4 하한·5 하락 → `-`, 3 보합 → 0.

---

## 9~10. 뉴스

### ① 종목 뉴스 크롤 (`news/crawler.py`) — 최근 ~5일

```
GET https://finance.naver.com/item/news_news.naver?code={CODE}&page=N
Referer: https://finance.naver.com/item/main.naver?code={CODE}
```

`table.type5` 각 행에서 제목/URL/언론사/날짜/요약 추출. 인코딩은 **euc-kr을 원문에서
직접 디코드**(meta 태그 감지에 맡기지 않음).

- 일시적 DNS/네트워크 이슈 대비 **3회 재시도(1.5초 간격)**.
- `since` 를 주면 그보다 오래된 기사를 만나는 즉시 중단(early stop) — 네이버 뉴스는
  최신순 정렬이라 이후 페이지는 전부 오래된 기사다.
- `NEWS_RELEVANCE_FILTER=true` 면 제목에 종목명/코드가 없는 기사를 버린다.

관련 파라미터: `NEWS_ENABLED`, `NEWS_CRAWL_INTERVAL_MINUTES`(기본 60),
`NEWS_PAGES_PER_SYMBOL`, `NEWS_LOOKBACK_HOURS`, `NEWS_MIN_ARTICLES`.

### ② 개발자 검색 API (`news/naver_api.py`) — 최대 30일, 백필용

```
GET https://openapi.naver.com/v1/search/news.json
X-Naver-Client-Id / X-Naver-Client-Secret
```

`display` 1..100 · `start` 1..1000 → **쿼리당 최대 1000건**. `sort=sim|date`.
`pubDate` 는 RFC1123.

**이 레포에서 유일하게 키가 필요한 네이버 접점**이다
(`NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET`).

`news/backfill.py --source {naver_item|naver_api}` 로 고른다. 기본은 `naver_api`
(종목페이지는 최근 ~5일, API는 최대 30일).

---

## 11~13. 이름·검색·시장 (`names.py`, `naver_index.py`)

| 용도 | 엔드포인트 |
|------|-----------|
| 종목명 | `finance.naver.com/item/main.naver?code=` |
| 검색 자동완성 | `m.stock.naver.com/front-api/search/autoComplete?query=&target=stock` |
| 상장시장 | `m.stock.naver.com/api/stock/{code}/basic` |

검색은 **입력 시점에만 1회 호출**한다(전체 마스터를 메모리에 들지 않는다).
코스피/코스닥만 남기고(KONEX·해외·지수 제외) 정확 일치 이름을 맨 앞에 둔다.
실패/오프라인이면 빈 리스트.

---

## 14. EPS·PER 컨센서스 — WISEreport (`screener.py`)

```
GET https://navercomp.wisereport.co.kr/company/cF1002.aspx?cmp_cd={CODE}&finGubun=0
Referer: .../v2/company/c1050001.aspx?cmp_cd={CODE}
X-Requested-With: XMLHttpRequest
```

`pd.read_html` 로 실적 EPS 시계열·포워드 EPS·실적/포워드 PER 을 뽑는다.
**`X-Requested-With` 헤더가 없으면 응답이 비어 온다.**

---

## 차단 회피 — 스크리너의 전역 스로틀

네이버는 **IP당 분당 요청 상한(~60~100건)** 초과 시 5~30분 차단한다. 유니버스가
커질수록(코스피/코스닥 각 1000) 총요청이 늘어 이 스로틀이 차단을 막는 핵심이 된다.

`screener.py:580~` 의 대응 4종:

1. **전역 rate limiter** — 모든 워커가 공유하는 락 + 최소간격 + 지터.
   `SCREENER_NAVER_RPM`(기본 **80** = 안전구간 하단)으로 조절.
2. **`requests.Session` keep-alive** — 커넥션 재사용(pool 4/8)으로 더 자연스럽고 빠르게.
3. **브라우저 헤더** — `User-Agent` + **`Referer`**(없으면 403) + `Accept-Language`.
4. **429/403/5xx 백오프 재시도.**

> **`Referer` 는 옵션이 아니다.** `finance.naver.com` 계열은 리퍼러 없는 요청에
> 403을 돌려준다. 모든 모듈이 `Referer: https://finance.naver.com/` 를 기본으로 단다.

---

## 공통 규칙 (새 크롤을 추가할 때 지킬 것)

1. **인코딩은 euc-kr.** `finance.naver.com` HTML 페이지는 전부 그렇다.
   `r.encoding = "euc-kr"` 또는 `r.content.decode("euc-kr", errors="replace")`.
2. **절대 예외를 올리지 않는다.** 라이브 러너가 도는 중이다. 실패는 빈 값
   (`[]` / `{}` / `""` / `set()`)으로 흡수하고 호출측이 직전값을 유지한다.
3. **빈 결과는 캐시하지 않는다.** 일시 실패를 굳히면 그날(또는 영원히) 그 항목이 사라진다.
4. **캐시 키에 날짜를 넣을지 결정하라.** 하루 안에 안 변하는 것(테마 구성종목)은
   날짜 키, 상장 기간 내내 안 변하는 것(업종·상장시장)은 영구 키, 실시간 값
   (테마 등락률·거래대금)은 **캐시하지 않는다**.
   → 날짜 키 캐시 파일은 `git rm --cached` 로 추적을 풀어야 한다.
   `.gitignore` 만으로는 안 풀리고, 충돌 대신 "선별이 느려짐"으로 위장한다.
5. **`Referer` 를 반드시 단다.**
6. **매매 판단에 쓸 것과 표시용을 구분하라.** `naver_quote` 는 표시 전용,
   `naver_minute` 은 워밍업 전용(VWAP·ATR 금지)이다.
7. **`pd.read_html` 결과의 행 수를 정규식 결과와 맞다고 가정하지 마라.**
   네이버 테이블에는 NaN 간격행이 섞여 있다(§1 함정 ①).

---

## 캐시 파일 목록

| 파일 | 내용 | 만료 |
|------|------|------|
| `data/leader_theme_cache.json` | 테마번호 → 종목코드 집합 | 날짜 키 (당일) |
| `data/leader_sector_cache.json` | 종목코드 → 업종명, 업종번호 → 업종명 | 없음 (영구) |
| `data/leader_avgval_cache.json` | 평균 거래대금 (창 길이 `w` 포함) | 날짜 키 |
| `data/leader_trend_cache.json` | 일봉 추세 | 날짜 키 |
| `data/leader_market_flow.json` | 시장 자금흐름 | 날짜 키 |

`leader_finder.py` 종료 시 `_save_theme_cache()` / `_save_sector_cache()` 를 호출해,
프리페치가 못 돈 날에도 첫 회차의 크롤 결과를 재시도 회차가 재사용하게 한다.

---

## 관련 문서

- [`docs/leader_reval_flow.md`](leader_reval_flow.md) — 재선별·섹터전환 흐름
- `leader_finder.py:62~72` — 거래대금 순위 소스 우선순위 정책 주석
