"""Claude Code 헤드리스(CLI) 백엔드 — Claude API 대신 구독 기반 호출.

`claude -p` 를 subprocess 로 호출해 최종 result 텍스트를 돌려준다. 라즈베리파이에
Claude Code CLI + CLAUDE_CODE_OAUTH_TOKEN 이 설정돼 있으면 ANTHROPIC_API_KEY 없이
동작하고 사용료가 0 이다(구독에 포함). 실패(미설치·타임아웃·토큰없음·비정상종료) 시
None 을 반환하는 fail-safe 구조 — 호출부는 기존 키워드/유지 폴백으로 자연히 떨어진다.

백엔드 스위치: 환경변수 LLM_BACKEND
  - "api" (기본)        : 기존 anthropic SDK 경로 사용 (롤백/안전 기본값)
  - "claude_code"       : 이 모듈(CLI)로 호출 — 파이에서 CLI+토큰 확인 후 전환

전환은 무중단 — LLM_BACKEND 만 바꾸면 다음 호출부터 즉시 반영된다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from loguru import logger


def backend() -> str:
    """현재 LLM 백엔드 ("api" | "claude_code").

    settings.llm_backend 를 우선한다 — 파라미터탭에서 저장하면 .env.overrides 핫리로드로
    settings 만 갱신되고 os.environ 은 그대로이므로(도커에서 고정), settings 를 봐야
    무중단 전환이 반영된다. settings 로드 실패 시 os.environ 으로 폴백.
    """
    try:
        from stock_bot.config.settings import settings
        val = getattr(settings, "llm_backend", None)
        if val:
            return str(val).strip().lower()
    except Exception:
        pass
    return (os.environ.get("LLM_BACKEND", "api") or "api").strip().lower()


def use_cli() -> bool:
    """Claude Code CLI 백엔드 사용 여부."""
    return backend() == "claude_code"


def _claude_bin() -> str | None:
    # 명시 경로 우선 (도커/파이 환경에서 PATH 미포함 대비)
    p = os.environ.get("CLAUDE_CODE_BIN")
    if p and os.path.exists(p):
        return p
    return shutil.which("claude")


def call_cli(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    allow_web: bool = False,
    timeout: float = 120.0,
) -> str | None:
    """Claude Code 헤드리스 호출. 성공 시 최종 텍스트, 실패 시 None(fail-safe).

    - prompt 는 stdin 으로 전달(길이·이스케이프 안전).
    - system 은 --append-system-prompt 로 주입(우리 페르소나·출력형식 강제).
    - allow_web=True 면 WebSearch 툴만 허용, 아니면 툴 없이 순수 추론.
    - model 은 CLI 별칭("sonnet"/"haiku"/"opus") 또는 전체 모델 ID.
    """
    bin_ = _claude_bin()
    if not bin_:
        logger.warning("claude CLI 미발견 — LLM 호출 건너뜀 (fail-safe)")
        return None

    cmd = [bin_, "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if system:
        cmd += ["--append-system-prompt", system]
    if allow_web:
        cmd += ["--allowedTools", "WebSearch"]

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("claude CLI 타임아웃({}s) — 건너뜀", timeout)
        return None
    except Exception as exc:
        logger.warning("claude CLI 실행 실패: {}", exc)
        return None

    if proc.returncode != 0:
        logger.warning("claude CLI 비정상 종료 rc={} err={}",
                       proc.returncode, (proc.stderr or "")[:200])
        return None

    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except Exception:
        # --output-format json 이 아닌 평문이 나온 경우 그대로 사용
        return out
    if isinstance(data, dict):
        if data.get("is_error"):
            logger.warning("claude CLI result 오류: {}", str(data.get("result", ""))[:200])
            return None
        res = data.get("result")
        if isinstance(res, str) and res.strip():
            return res.strip()
    return None
