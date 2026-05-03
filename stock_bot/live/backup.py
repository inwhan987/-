"""일별 데이터 백업: TradeLog + ReviewLog + NewsRow → CSV → git push.

매일 자정(00:05 KST)에 실행:
  1. trades.db 에서 TradeLog / ReviewLog 전체를 CSV로 내보냄
  2. news.db 에서 어제 날짜 기사를 날짜별 CSV로 내보냄
  3. data/ 폴더에 저장 (git 추적 대상)
  4. git add → commit → push

data/ 폴더 구조:
  data/trades.csv           — 전체 체결 내역
  data/reviews.csv          — 전체 장마감 리뷰
  data/news/2026-05-01.csv  — 날짜별 뉴스 + 감성점수
  data/backup_log.txt       — 백업 실행 기록
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from stock_bot.storage import ENGINE, TradeLog, ReviewLog
from stock_bot.news.store import NEWS_ENGINE, NewsRow
from stock_bot.notify import notify

_KST = timezone(timedelta(hours=9))
_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _ROOT / "data"
_NEWS_DIR = _DATA_DIR / "news"


def _export_trades(path: Path) -> int:
    """TradeLog 전체 → CSV. 행 수 반환."""
    with Session(ENGINE) as s:
        rows = s.scalars(select(TradeLog).order_by(TradeLog.ts)).all()

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "ts_kst", "symbol", "side", "quantity",
                         "price", "strategy", "reason"])
        for r in rows:
            kst = r.ts.replace(tzinfo=timezone.utc).astimezone(_KST)
            writer.writerow([
                r.id,
                kst.strftime("%Y-%m-%d %H:%M:%S"),
                r.symbol, r.side, r.quantity, r.price,
                r.strategy,
                r.reason[:120].replace("\n", " "),
            ])
    return len(rows)


def _export_reviews(path: Path) -> int:
    """ReviewLog 전체 → CSV. 행 수 반환."""
    with Session(ENGINE) as s:
        rows = s.scalars(select(ReviewLog).order_by(ReviewLog.date)).all()

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "date", "trades_count", "summary",
                         "findings_count", "suggestions_count"])
        for r in rows:
            try:
                findings = json.loads(r.findings) if r.findings else []
                suggestions = json.loads(r.suggestions) if r.suggestions else []
            except Exception:
                findings, suggestions = [], []
            writer.writerow([
                r.id, r.date, r.trades_count,
                r.summary[:200].replace("\n", " "),
                len(findings), len(suggestions),
            ])
    return len(rows)


def _export_news(date_str: str) -> int:
    """어제 날짜 뉴스 기사 → data/news/YYYY-MM-DD.csv. 행 수 반환."""
    day_start = datetime.strptime(date_str, "%Y-%m-%d")
    day_end = day_start + timedelta(days=1)

    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow)
            .where(NewsRow.published_at >= day_start)
            .where(NewsRow.published_at < day_end)
            .order_by(NewsRow.published_at)
        ).all()

    if not rows:
        return 0

    _NEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = _NEWS_DIR / f"{date_str}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id", "symbol", "published_at", "publisher",
            "sentiment_score", "sentiment_method", "is_critical", "weight", "title",
        ])
        for r in rows:
            writer.writerow([
                r.id, r.symbol,
                r.published_at.strftime("%Y-%m-%d %H:%M"),
                r.publisher,
                round(r.sentiment_score, 4),
                r.sentiment_method,
                int(r.is_critical),
                round(r.weight, 2),
                r.title[:200],
            ])
    return len(rows)


def _git_push(message: str) -> bool:
    """git add data/ → commit → push. 성공 여부 반환."""
    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=str(_ROOT),
            capture_output=True, text=True, timeout=60,
        )

    # 컨테이너 내부에서 볼륨 마운트된 .git 소유자 불일치 방지
    _run(["git", "config", "--global", "--add", "safe.directory", str(_ROOT)])
    _run(["git", "config", "--global", "user.email", "stockbot@localhost"])
    _run(["git", "config", "--global", "user.name", "stock-bot"])

    # 변경사항 있는지 확인
    status = _run(["git", "status", "--porcelain", "data/"])
    if not status.stdout.strip():
        logger.debug("backup: data/ 변경 없음, git push 생략")
        return True

    r = _run(["git", "add", "data/"])
    if r.returncode != 0:
        logger.warning("backup git add 실패: {}", r.stderr[:200])
        return False

    r = _run(["git", "commit", "-m", message])
    if r.returncode != 0:
        logger.warning("backup git commit 실패: {}", r.stderr[:200])
        return False

    r = _run(["git", "push"])
    if r.returncode != 0:
        logger.warning("backup git push 실패: {}", r.stderr[:200])
        return False

    return True


def run_backup() -> None:
    """CSV 내보내기 + git push 실행."""
    _DATA_DIR.mkdir(exist_ok=True)

    now_kst = datetime.now(tz=_KST)
    today_str = now_kst.strftime("%Y-%m-%d")
    # 00:05 KST 실행 → 어제 날짜 뉴스를 백업
    yesterday_str = (now_kst - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        n_trades = _export_trades(_DATA_DIR / "trades.csv")
        n_reviews = _export_reviews(_DATA_DIR / "reviews.csv")
        n_news = _export_news(yesterday_str)
    except Exception as exc:
        logger.exception("backup CSV 내보내기 실패: {}", exc)
        notify(f"⚠️ 백업 실패 (CSV): {exc}")
        return

    # 백업 실행 기록
    with open(_DATA_DIR / "backup_log.txt", "a", encoding="utf-8") as f:
        f.write(
            f"{now_kst.strftime('%Y-%m-%d %H:%M:%S KST')} "
            f"trades={n_trades} reviews={n_reviews} news({yesterday_str})={n_news}\n"
        )

    commit_msg = (
        f"data: 일별 백업 {today_str} "
        f"(체결 {n_trades}건 / 리뷰 {n_reviews}건 / 뉴스 {n_news}건)"
    )

    ok = _git_push(commit_msg)
    if ok:
        logger.info(
            "backup 완료: trades={} reviews={} news={} → git push",
            n_trades, n_reviews, n_news,
        )
    else:
        logger.warning("backup CSV 저장 완료, git push 실패 (로컬엔 저장됨)")
        notify(
            f"⚠️ 백업 git push 실패 — 로컬 data/ 폴더엔 저장됨\n"
            f"체결 {n_trades}건 / 리뷰 {n_reviews}건 / 뉴스 {n_news}건"
        )
        return

    notify(
        f"💾 **일별 백업 완료** ({today_str})\n"
        f"체결 {n_trades}건 · 리뷰 {n_reviews}건 · 뉴스 {n_news}건 → GitHub 업로드"
    )
