# 스크리너 원격(CI) 스코어링 오프로드 — 설정 가이드

파이 OOM(~905Mi) 대책. 무거운 **스코어링(③)** 만 GitHub Actions 로 넘기고, CI가
`screener.py` stdout 을 **중계 gist 에 누적 PATCH** → 파이가 그 gist 를 폴링해 실시간
수신한다. 파이 웹 로그탭·날짜별 파일 저장은 **로컬 실행과 100% 동형**. 섹터선정(①)·
섹터검수(②-a)·종목검수(②-b)·SYMBOLS 기록은 파이가 그대로 처리한다.

**터널/도메인/포트개방 0** — 파이는 아웃바운드로 GitHub API 만 부른다.

```
파이  : ① 섹터랭킹 → ②-a 섹터검수(LLM, claude_code 구독)   [가벼움]
  │        ├ 빈 중계 gist 생성 (gist_id 확보)
  │        └ screener-run.yml 을 workflow_dispatch 로 트리거 (gist_id + run_token 전달)
CI    : ③ 스코어링 (KRX 로그인·Yahoo 1년봉·DART×1900)        [무거움 → 오프로드]
  │        └ stdout 을 그 gist 에 누적 PATCH (2초 간격, 전체 교체)
파이  : gist 를 ~2초 폴링 → 증분을 로컬과 동일 consumer 로 흘림
  │        → ②-b 종목검수(LLM) → SYMBOLS 기록. 끝나면 gist 삭제.
```

토글 OFF(기본) 또는 PAT 없으면 **자동으로 기존 로컬 실행으로 폴백** —
이 커밋만으로는 파이 동작이 바뀌지 않는다.

---

## 1. GitHub PAT 만들기 (gist 권한 필수)

gist 는 **레포가 아니라 계정 단위**라 CI 기본 `GITHUB_TOKEN` 으론 못 쓴다. 그래서
파이(생성/폴링/삭제)와 CI(쓰기) 둘 다 gist 권한 PAT 가 필요하다. **classic PAT** 하나로
양쪽을 커버하는 게 제일 간단하다:

- GitHub → Settings → Developer settings → **Personal access tokens (classic)** → Generate
- 스코프 체크: **`repo`** + **`workflow`** + **`gist`**
- (fine-grained PAT 는 현재 gist 접근을 지원하지 않으므로 classic 을 쓴다)

이 토큰 값 하나를 아래 2·3 양쪽에 넣는다.

---

## 2. GitHub 레포 시크릿 (repo `inwhan987/-` → Settings → Secrets → Actions)

이미 설정됨: `DART_API_KEY`, `KRX_ID`, `KRX_PW`.
추가로 하나:

| 이름 | 값 |
|---|---|
| `SCREENER_GIST_TOKEN` | 1번 classic PAT (CI가 gist 에 로그 쓰기용) |

값 노출 없이 넣기 (파이 셸에서):
```bash
# 클립보드/파일 대신 파이프로 바로 주입 — 화면에 값이 안 뜨게
printf '%s' '<PAT>' | gh secret set SCREENER_GIST_TOKEN --repo inwhan987/-
```

> 과거의 `SCREENER_CI_INGEST_SECRET` 시크릿은 **더 이상 안 쓴다** — 삭제해도 된다.

---

## 3. 파이 `.env` (시크릿 — `.env.overrides` 금지)

```dotenv
SCREENER_GH_TOKEN=ghp_...            # 1번 classic PAT (repo·workflow·gist)
# 선택(기본값 있음):
# SCREENER_CI_REPO=inwhan987/-
# SCREENER_CI_REF=main
# SCREENER_CI_WORKFLOW=screener-run.yml
```

> 예전의 `SCREENER_CI_INGEST_SECRET`·`SCREENER_CI_CALLBACK_URL` 은 **삭제**한다.
> cloudflared/ngrok 등 터널·systemd 세팅도 전부 **불필요** — 통째로 걷어낸다.

---

## 4. 켜기

파이 `.env.overrides`:
```dotenv
SCREENER_REMOTE_ENABLED=true
```
그리고 `docker compose up -d` (또는 웹 재기동). 이후 스크리너 실행 시 로그탭에
`[CI 디스패치 → 러너 부팅...]` 이 뜨고, 30~60초 뒤 CI 스코어링 로그가 gist 폴링으로
실시간(~2초 지연) 흐른다.

---

## 5. 점검 체크리스트

- [ ] 1번 PAT 스코프에 `gist` 포함 (없으면 gist 생성 403 → job error)
- [ ] `SCREENER_GH_TOKEN` 파이 `.env` == 1번 PAT (repo·workflow·gist)
- [ ] 레포 시크릿 `SCREENER_GIST_TOKEN` 설정됨 (= 1번 PAT)
- [ ] Actions 탭에서 `screener-run` 워크플로우가 보임(= main 에 반영됨)
- [ ] 로그탭에 CI 스트림이 흐르고, 완료 후 종목검수·SYMBOLS 갱신까지 정상

## 실패 시 동작
- gist 생성/디스패치 실패(토큰/권한/네트워크) → 스크리너 job `error` + Discord 알림.
  로컬 폴백은 자동이 아니므로, 급하면 `SCREENER_REMOTE_ENABLED=false` 로 즉시 로컬 복귀.
- CI 30분 초과 → job `error` + "CI 타임아웃" 알림.
- 취소(/cancel) → GitHub 런 취소 요청 + 파이 폴링 중단 + 중계 gist 삭제.
