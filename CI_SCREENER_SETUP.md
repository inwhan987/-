# 스크리너 원격(CI) 스코어링 오프로드 — 설정 가이드

파이 OOM(~905Mi) 대책. 무거운 **스코어링(③)** 만 GitHub Actions 로 넘기고, 러너가
`screener.py` stdout 을 **터널로 파이에 실시간 스트리밍** → 파이 웹 로그탭·날짜별 파일
저장이 **로컬 실행과 100% 동형**. 섹터선정(①)·섹터검수(②-a)·종목검수(②-b)·SYMBOLS
기록은 파이가 그대로 처리한다.

```
파이  : ① 섹터랭킹 → ②-a 섹터검수(LLM, claude_code 구독)   [가벼움]
  │        └ screener-run.yml 을 workflow_dispatch 로 트리거 (run_token + 터널 URL 전달)
CI    : ③ 스코어링 (KRX 로그인·Yahoo 1년봉·DART×1900)        [무거움 → 오프로드]
  │        └ stdout 라인들을 터널 → 파이 /api/screener/ingest 로 실시간 POST
파이  : (스트림 수신 = 로컬과 동일 consumer) → ②-b 종목검수(LLM) → SYMBOLS 기록
```

토글 OFF(기본) 또는 자격증명 하나라도 없으면 **자동으로 기존 로컬 실행으로 폴백** —
이 커밋만으로는 파이 동작이 바뀌지 않는다.

---

## 1. GitHub 시크릿 (repo `inwhan987/-` → Settings → Secrets → Actions)

이미 설정됨: `DART_API_KEY`, `KRX_ID`, `KRX_PW`.
추가로 하나:

| 이름 | 값 |
|---|---|
| `SCREENER_CI_INGEST_SECRET` | 임의의 긴 랜덤 문자열 (파이 `.env` 와 **동일 값**) |

생성 예 (값 노출 없이):
```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
# 출력값을 GitHub secret 과 파이 .env 양쪽에 같은 값으로 넣는다
```

---

## 2. 파이 `.env` (시크릿 — `.env.overrides` 금지)

```dotenv
SCREENER_GH_TOKEN=ghp_...            # GitHub PAT, 권한: repo inwhan987/- 의 actions:write
SCREENER_CI_INGEST_SECRET=<위 1번과 동일한 값>
SCREENER_CI_CALLBACK_URL=https://<파이-터널-도메인>     # 3번 참고 (경로 없이 베이스만)
# 선택(기본값 있음):
# SCREENER_CI_REPO=inwhan987/-
# SCREENER_CI_REF=main
# SCREENER_CI_WORKFLOW=screener-run.yml
```

PAT: fine-grained PAT 권장 — Repository access = `inwhan987/-`, Permissions →
**Actions: Read and write**. classic PAT 이면 `repo` 스코프.

---

## 3. 터널 (CI → 파이 인바운드용) — cloudflared 권장

파이는 외부에서 접속 불가(LAN)라 CI 가 로그를 밀어넣으려면 파이에 공개 엔드포인트가
필요하다. `named tunnel` 을 쓰면 **URL 이 고정**되어 재부팅에도 안 바뀐다(권장).

빠른 테스트(임시 URL, 재시작마다 바뀜):
```bash
cloudflared tunnel --url http://localhost:8001
# 출력된 https://xxxx.trycloudflare.com 을 SCREENER_CI_CALLBACK_URL 에
```

운영(고정 URL):
```bash
cloudflared tunnel login
cloudflared tunnel create stock-web
# config.yml 에 ingress: http://localhost:8001 매핑 + DNS route
cloudflared tunnel route dns stock-web screener.<your-domain>
cloudflared tunnel run stock-web          # systemd 서비스로 상시 기동 권장
# SCREENER_CI_CALLBACK_URL=https://screener.<your-domain>
```

> 터널은 **레포 밖(파이 호스트)** 설정이라, 파이 재구축 시 재적용 필요
> ([[pi-oom-mitigation]] 와 동일 주의). 터널로 여는 건 8001 포트뿐이며, ingest
> 는 `X-Ingest-Secret` 헤더로 보호된다(시크릿 없으면 403).

---

## 4. 켜기

파이 `.env.overrides`:
```dotenv
SCREENER_REMOTE_ENABLED=true
```
그리고 `docker compose up -d` (또는 웹 재기동). 이후 스크리너 실행 시 로그탭에
`[CI 디스패치 → 러너 부팅...]` 이 뜨고, 30~60초 뒤 CI 스코어링 로그가 실시간으로 흐른다.

---

## 5. 점검 체크리스트

- [ ] `SCREENER_CI_INGEST_SECRET` 파이 `.env` == GitHub secret (동일 값)
- [ ] `SCREENER_GH_TOKEN` 이 `inwhan987/-` 에 actions:write 권한
- [ ] 터널 상시 기동 + `SCREENER_CI_CALLBACK_URL` 이 그 도메인(베이스, 경로 없음)
- [ ] Actions 탭에서 `screener-run` 워크플로우가 보임(= main 에 반영됨)
- [ ] 로그탭에 CI 스트림이 흐르고, 완료 후 종목검수·SYMBOLS 갱신까지 정상

## 실패 시 동작
- 디스패치 실패(토큰/권한/네트워크) → 스크리너 job `error` + Discord 알림. 로컬 폴백은
  자동이 아니므로, 급하면 `SCREENER_REMOTE_ENABLED=false` 로 즉시 로컬 복귀.
- CI 30분 초과 → job `error` + "CI 타임아웃" 알림.
