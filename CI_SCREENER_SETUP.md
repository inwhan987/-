# 스크리너 원격(CI) 스코어링 오프로드 — 설정 가이드

파이 OOM(~905Mi) 대책. 무거운 **스코어링(③)** 만 GitHub Actions 로 넘기고, CI가
`screener.py` stdout(누적 전체 로그)을 파이로 실시간 중계한다. 파이 웹 로그탭·날짜별
파일 저장은 **로컬 실행과 100% 동형**. 섹터선정(①)·섹터검수(②-a)·종목검수(②-b)·
SYMBOLS 기록은 파이가 그대로 처리한다.

전송 방식은 두 가지 — 어느 쪽이든 소비 로직은 동일(누적 전체 → `content[_consumed:]` 증분):

- **(A) 터널 push (기본·권장, 실시간):** 파이가 **cloudflared quick tunnel**(무료·계정/
  도메인 불필요)로 인바운드 URL 을 뚫고 그 URL 을 CI 디스패치에 실어보낸다. CI가 로그를
  파이 `/api/screener/ingest` 로 **직접 POST** → 파이 즉시 소비. **GitHub API 한도와
  완전 무관**하고 폴링 지연이 없다(실시간).
- **(B) gist 폴백:** 터널 URL 이 없으면(파일 미존재) CI가 비공개 gist 에 PATCH → 파이가
  raw_url 폴링. 터널이 꺼져 있어도 스크리너는 동작한다.

```
파이  : ① 섹터랭킹 → ②-a 섹터검수(LLM, claude_code 구독)   [가벼움]
  │        ├ data/tunnel_url.txt 에서 현재 터널 URL 읽기 (있으면 push, 없으면 gist)
  │        └ screener-run.yml 디스패치 (callback_url 또는 gist_id + run_token)
CI    : ③ 스코어링 (KRX 로그인·Yahoo 1년봉·DART×1900)        [무거움 → 오프로드]
  │        └ stdout 누적 전체를 파이로 POST(2초) 또는 gist PATCH(20초)
파이  : 수신 → 로컬과 동일 consumer 로 흘림 → ②-b 종목검수(LLM) → SYMBOLS 기록
```

토글 OFF(기본) 또는 PAT 없으면 **자동으로 기존 로컬 실행으로 폴백**.

---

## 0. cloudflared quick tunnel (터널 push 모드 — 무료, 계정/도메인 불필요)

파이는 LAN-only라 CI→파이 인바운드를 받으려면 터널이 필요하다. **quick tunnel** 은
계정도 도메인도 없이 `*.trycloudflare.com` URL 을 즉석에서 준다. 재시작마다 URL 이
바뀌지만, 파이가 디스패치 때마다 현재 URL 을 파일에서 읽어 넘기므로 문제없다.

**① 설치** (아키텍처 확인 후 — `uname -m`: `aarch64`=arm64, `armv7l`=arm):
```bash
# 64비트(aarch64) 예시:
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -O cloudflared
chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/cloudflared
cloudflared --version
```

**② 상시 실행 + URL 자동기록** — 레포에 러너 스크립트가 있다(`scripts/cloudflared_tunnel.sh`).
현재 URL 을 `data/tunnel_url.txt`(컨테이너 `/app/data` 마운트)에 기록한다. systemd 로 등록:

```ini
# /etc/systemd/system/screener-tunnel.service
[Unit]
Description=cloudflared quick tunnel for screener CI ingest
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
# 레포 실제 경로로 바꾸기 (docker compose 의 ./data 가 마운트되는 그 디렉터리)
WorkingDirectory=/home/pi/<repo>
Environment=LOCAL_PORT=8001
ExecStart=/bin/bash /home/pi/<repo>/scripts/cloudflared_tunnel.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now screener-tunnel
# 확인: URL 이 기록됐는지
cat /home/pi/<repo>/data/tunnel_url.txt   # https://xxxx.trycloudflare.com 나와야 함
```

> URL 파일이 없거나 비면 파이는 자동으로 **gist 폴백**을 쓴다(아래 gist 설정 필요).
> 수동으로 콜백 URL 을 고정하고 싶으면 파이 `.env` 에 `SCREENER_CI_CALLBACK_URL=` 로
> 덮어쓸 수 있다(파일보다 우선).

---

## 1. GitHub PAT (워크플로우 디스패치 + gist 폴백용)

파이가 워크플로우를 `workflow_dispatch` 로 트리거하고 취소하려면 PAT 가 필요하다. gist
폴백까지 쓰려면 gist 권한도 필요하므로 **classic PAT** 하나로 다 커버하는 게 간단하다:

- GitHub → Settings → Developer settings → **Personal access tokens (classic)** → Generate
- 스코프: **`repo`** + **`workflow`** + **`gist`**
- (fine-grained PAT 는 gist 미지원 → classic)

---

## 2. GitHub 레포 시크릿 (repo `inwhan987/-` → Settings → Secrets → Actions)

이미 설정됨: `DART_API_KEY`, `KRX_ID`, `KRX_PW`.

| 이름 | 값 | 용도 |
|---|---|---|
| `SCREENER_CI_INGEST_SECRET` | 임의 랜덤 문자열 (`openssl rand -hex 16`) | **터널 push** — CI→파이 인바운드 검증 |
| `SCREENER_GIST_TOKEN` | 1번 classic PAT | **gist 폴백** — CI가 gist 쓰기 |

값 노출 없이 넣기 (`gh` 있는 PC/파이에서):
```bash
printf '%s' '<값>' | gh secret set SCREENER_CI_INGEST_SECRET --repo inwhan987/-
printf '%s' '<PAT>' | gh secret set SCREENER_GIST_TOKEN --repo inwhan987/-
```

---

## 3. 파이 `.env` (시크릿 — `.env.overrides` 금지)

```dotenv
SCREENER_GH_TOKEN=ghp_...              # 1번 classic PAT (repo·workflow·gist)
SCREENER_CI_INGEST_SECRET=<위와 동일>  # 터널 push 인바운드 검증 (레포 시크릿과 동일값)
# 선택(기본값 있음):
# SCREENER_CI_CALLBACK_URL=            # 비우면 data/tunnel_url.txt 자동 사용
# SCREENER_CI_REPO=inwhan987/-
# SCREENER_CI_REF=main
# SCREENER_CI_WORKFLOW=screener-run.yml
```

---

## 4. 켜기

파이 `.env.overrides`:
```dotenv
SCREENER_REMOTE_ENABLED=true
```
그리고 `docker compose up -d` (또는 웹 재기동). 이후 스크리너 실행 시 로그탭에
`[CI 디스패치(터널 실시간) → ...]`(push) 또는 `(gist 폴백)` 이 뜨고, 30~60초 뒤 CI
스코어링 로그가 실시간으로 흐른다.

---

## 5. 점검 체크리스트

- [ ] `uname -m` 확인 후 맞는 cloudflared 바이너리 설치, `cloudflared --version` OK
- [ ] `screener-tunnel` systemd active, `data/tunnel_url.txt` 에 `https://…trycloudflare.com` 기록됨
- [ ] 레포 시크릿 `SCREENER_CI_INGEST_SECRET` == 파이 `.env` 값
- [ ] 파이 `.env` `SCREENER_GH_TOKEN` = classic PAT(repo·workflow·gist)
- [ ] (gist 폴백 쓸 거면) 레포 시크릿 `SCREENER_GIST_TOKEN` 설정
- [ ] Actions 탭에 `screener-run` 워크플로우 보임(= main 반영됨)
- [ ] 로그탭에 CI 스트림이 실시간으로 흐르고, 완료 후 종목검수·SYMBOLS 갱신 정상

## 실패 시 동작
- 터널 URL 없음 → 자동 gist 폴백(gist 설정 필요). 둘 다 없으면 디스패치는 되나 수신 불가.
- 디스패치 실패(토큰/권한/네트워크) → job `error` + Discord 알림. 급하면
  `SCREENER_REMOTE_ENABLED=false` 로 즉시 로컬 복귀.
- CI 120분 초과 → job `error` + "CI 타임아웃" 알림.
- 취소(/cancel) → GitHub 런 취소 요청 + 파이 수신 중단 (+ gist 모드면 gist 삭제).
- ingest 는 로그 버퍼 갱신 외 아무 동작 안 함(시크릿 불일치 시 401). 인바운드 표면 최소.
