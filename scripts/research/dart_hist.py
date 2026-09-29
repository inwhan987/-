"""DART 과거 재무제표 1회성 수집 — quality/growth 축 포인트인타임 백필용.

왜 1회성인가: 분기 재무는 한 번 공시되면 불변이다. 그래서 (종목 × 재무기간) 단위로
원본을 한 번만 받아두면, 이후 어떤 as-of 날짜의 값이든 rcept_dt(접수일)로 필터링해서
오프라인에서 재구성할 수 있다. 날짜별로 다시 호출하지 않는다.

왜 dart.collect() 를 못 쓰는가: collect() 는 fetch_financials() 에 now 를 넘기지 않아
항상 "지금 분기"만 본다. 과거 시점 재구성이 불가능하다.

이 스크립트는 읽기 전용이다 — swing.db, dart_fin 테이블, swing_dart_cache.json 을
건드리지 않는다. 결과는 자체 JSONL 에만 쌓는다.

    python scripts/research/dart_hist.py --limit 5            # 스모크(약 1분)
    python scripts/research/dart_hist.py                      # 전체
    python scripts/research/dart_hist.py --sleep 0.2 --workers 3

중단해도 안전하다. 같은 명령을 다시 돌리면 JSONL 에 이미 있는 (code, year, rtype) 은
건너뛴다 — 실패한 실행이 호출 한도/CI 분을 재소모하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_bot.swing import dart  # noqa: E402

# DART_API_KEY 는 로컬 .env / CI 시크릿 어느 쪽에서 와도 된다. 이미 환경변수에
# 있으면 그것을 쓴다(load_dotenv 기본 override=False) → CI 에서는 .env 가 없어도 된다.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

OUT_PATH = ROOT / "data" / "research" / "dart_hist.jsonl"
CODES_PATH = Path(__file__).with_name("dart_hist_codes.json")

# 백테스트 as-of 구간(2024-01-01 ~ 현재)에서 fetch_financials() 가 실제로 조회하게 되는
# (연도, 보고서종류) 조합 전부. _qtr_candidates() 를 그 구간의 모든 월에 대해 전개하고,
# 분기는 전년 동기 비교분까지 포함시켜 도출했다. 2026 Q3 는 아직 미공시라 013 이 오는데,
# 그래도 남겨둔다 — 11월 이후 같은 명령을 다시 돌리면 그때 채워진다.
ANNUAL_YEARS = (2022, 2023, 2024, 2025)
QTR_YEARS = (2022, 2023, 2024, 2025, 2026)
QTR_TYPES = ("11013", "11012", "11014")   # Q1, H1, Q3


def targets() -> list[tuple[int, str]]:
    out = [(y, dart.RTYPE_ANNUAL) for y in ANNUAL_YEARS]
    out += [(y, t) for y in QTR_YEARS for t in QTR_TYPES]
    return out


def extract(rows: list[dict] | None) -> dict | None:
    """원본 행에서 재구성에 필요한 숫자만 뽑는다. 파생지표는 저장하지 않는다 —
    성장률 공식이 바뀌어도 재수집 없이 다시 계산할 수 있어야 한다."""
    fs = dart._pick_div(rows)
    if not fs:
        return None
    return {
        "rcept_dt": dart._rcept_dt(fs),
        "fs_div": fs[0].get("fs_div"),
        "rev_th": dart._amount(fs, *dart._REV_KEYS),
        "rev_fr": dart._amount(fs, *dart._REV_KEYS, col="frmtrm_amount"),
        "inc_th": dart._amount(fs, *dart._INC_KEYS),
        "inc_fr": dart._amount(fs, *dart._INC_KEYS, col="frmtrm_amount"),
        "equity": dart._amount(fs, "자본총계"),
        "debt": dart._amount(fs, "부채총계"),
        "nrows": len(fs),
    }


def resolve_corp(codes: list[str], corp: dict[str, str]) -> dict[str, str]:
    """종목 → corp_code. 우선주(코드 끝자리 5/K)는 DART 공시가 따로 없고 본주 재무를
    그대로 쓴다 → 본주 코드(code[:5]+"0")의 corp_code 로 폴백한다. 544종목 중 11개가
    우선주였고 본주가 모두 목록 안에 있으므로, 호출을 corp_code 단위로 중복제거하면
    이 폴백 때문에 호출이 늘지 않는다(추가 0건)."""
    out: dict[str, str] = {}
    for c in codes:
        cc = corp.get(c) or corp.get(c[:5] + "0")
        if cc:
            out[c] = cc
    return out


def load_done() -> set[tuple[str, int, str]]:
    done: set[tuple[str, int, str]] = set()
    if not OUT_PATH.exists():
        return done
    with OUT_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue          # 중단으로 잘린 마지막 줄 — 그 한 건만 다시 받는다
            done.add((r["code"], int(r["year"]), r["rtype"]))
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="종목 수 제한(스모크용)")
    ap.add_argument("--sleep", type=float, default=0.2, help="호출 간격(초)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--codes-file", default=str(CODES_PATH))
    a = ap.parse_args()

    # dart.py 의 상수를 이 프로세스에서만 바꾼다. 파일은 수정하지 않는다 —
    # 주간 배치는 계속 0.5초로 돈다.
    dart._SLEEP = a.sleep

    codes: list[str] = json.loads(Path(a.codes_file).read_text(encoding="utf-8"))
    if a.limit:
        codes = codes[: a.limit]
    corp = dart.load_corp_map()
    tgts = targets()

    done = load_done()
    cmap = resolve_corp(codes, corp)

    # 호출은 (corp_code, 재무기간) 단위 — 우선주와 본주는 같은 재무를 공유하므로
    # 같은 corp_code 를 두 번 때리지 않고, 결과를 해당 종목들에 함께 기록한다.
    jobs: dict[tuple[str, int, str], list[str]] = {}
    for c in codes:
        cc = cmap.get(c)
        if not cc:
            continue
        for (y, t) in tgts:
            if (c, y, t) not in done:
                jobs.setdefault((cc, y, t), []).append(c)
    missing = sorted(c for c in codes if c not in cmap)
    print(f"종목 {len(codes)}개 / corp_code 없음 {len(missing)}개 "
          f"/ 재무기간 {len(tgts)}건 / 이미받음 {len(done)}건 / 호출예정 {len(jobs)}건",
          flush=True)
    if missing[:10]:
        print(f"  corp_code 미확인: {missing[:10]}", flush=True)
    if not jobs:
        print("받을 것이 없다.", flush=True)
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    stats = {"ok": 0, "nodata": 0, "error": 0}
    stop = threading.Event()

    def work(job: tuple[str, int, str]) -> tuple[tuple[str, int, str], dict | None, str]:
        corp_code, year, rtype = job
        if stop.is_set():
            return job, None, "aborted"
        try:
            rows = dart._finstate(corp_code, year, rtype)
        except dart.DartError as exc:
            if "020" in str(exc):
                stop.set()        # 일일 한도 초과 — 더 때려도 소용없다
            return job, None, str(exc)
        except Exception as exc:  # noqa: BLE001
            return job, None, repr(exc)
        return job, extract(rows), ""

    with OUT_PATH.open("a", encoding="utf-8") as out, \
            ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(work, j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            job, data, err = fut.result()
            corp_code, year, rtype = job
            if err == "aborted":
                continue
            if err:
                stats["error"] += 1
                if stats["error"] <= 20:
                    print(f"  ! {corp_code} {year} {rtype}: {err}", flush=True)
                continue
            if data is None:
                stats["nodata"] += 1
            else:
                stats["ok"] += 1
            with lock:
                for code in jobs[job]:
                    rec = {"code": code, "year": year, "rtype": rtype}
                    if data is None:
                        # 없다는 사실도 남긴다 → 재실행이 다시 안 부른다
                        rec["nodata"] = True
                    else:
                        rec.update(data)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if i % 500 == 0:
                    out.flush()
                    print(f"  {i}/{len(jobs)} ok={stats['ok']} "
                          f"nodata={stats['nodata']} err={stats['error']}", flush=True)

    print(f"완료: ok={stats['ok']} nodata={stats['nodata']} err={stats['error']} "
          f"→ {OUT_PATH.relative_to(ROOT)}", flush=True)
    if stop.is_set():
        print("DART 일일 호출 한도(020)로 중단됐다. 내일 같은 명령을 다시 돌리면 이어받는다.",
              flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
