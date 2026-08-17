#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AVWAP 프로브 로그 분석.

    python avwap_report.py --logs data/avwap_probe

출력
  [1] 변형별 신호 수 — 세션 VWAP 대비 AVWAP이 몇 배 신호를 주는가
  [2] 진입 퍼널 — 어느 조건이 신호를 죽이는가 (장대양봉컷 학살률 포함)
  [3] 앵커 타이밍 분포 — 임계값이 적절한가 (너무 늦게/일찍 잡히는가)
  [4] 손절갭 분포 — R:R 실측, 앞서 계산한 1.5~3.0% 범위와 대조
"""
import argparse, glob, json, os, statistics, sys
from collections import defaultdict, Counter


def load(pattern):
    files = sorted(glob.glob(pattern))
    recs = []
    for p in files:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return recs, files


def pct(a, b):
    return f"{a / b * 100:5.1f}%" if b else "    —"


def dist(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    q = lambda p: s[min(n - 1, int(n * p))]
    return dict(n=n, min=s[0], p25=q(.25), med=q(.50), p75=q(.75),
                p90=q(.90), max=s[-1], mean=sum(s) / n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default="data/avwap_probe")
    args = ap.parse_args()

    pat = args.logs if any(c in args.logs for c in "*?") \
        else os.path.join(args.logs, "*.jsonl")
    recs, files = load(pat)
    if not recs:
        sys.exit(f"로그 없음: {pat}")

    days = sorted({r["date"] for r in recs})
    variants = []
    for r in recs:
        for k in r.get("variants", {}):
            if k not in variants:
                variants.append(k)

    W = 76
    print("\n" + "=" * W)
    print(f"  AVWAP 프로브 리포트  ·  {len(days)}일  ·  관측 {len(recs):,}건"
          f"  ·  파일 {len(files)}개")
    print(f"  기간: {days[0]} ~ {days[-1]}")
    print("=" * W)

    # ── [1] 변형별 신호 수 ──────────────────────────────
    print("\n[1] 변형별 신호 수")
    print(f"  {'변형':<14}{'최종신호':>9}{'일평균':>8}{'대조군대비':>11}"
          f"{'앵커확정율':>11}")
    print("  " + "-" * 55)
    base_sig = sum(1 for r in recs
                   if r["variants"].get("session", {}).get("signal"))
    for v in variants:
        sig = sum(1 for r in recs if r["variants"].get(v, {}).get("signal"))
        has_anchor = sum(1 for r in recs
                         if r["variants"].get(v, {}).get("anchor_idx") is not None)
        ratio = f"{sig / base_sig:.2f}x" if base_sig else "—"
        label = "세션(대조군)" if v == "session" else v
        print(f"  {label:<14}{sig:>9}{sig / len(days):>8.2f}{ratio:>11}"
              f"{pct(has_anchor, len(recs)):>11}")

    # ── [2] 퍼널 ────────────────────────────────────────
    print("\n[2] 진입 퍼널 — 어디서 죽는가")
    for v in variants:
        rows = [r["variants"][v] for r in recs if v in r["variants"]]
        rows = [x for x in rows if x.get("anchor_idx") is not None]
        if not rows:
            continue
        n = len(rows)
        trend = sum(1 for x in rows if x["trend"])
        touch = sum(1 for x in rows if x["trend"] and x["touch"])
        recl = sum(1 for x in rows if x["raw_signal"])
        final = sum(1 for x in rows if x["signal"])
        killed = recl - final
        label = "세션(대조군)" if v == "session" else v
        print(f"\n  ── {label} ──")
        print(f"    관측               {n:>7}")
        print(f"    (a) 상승추세 통과   {trend:>7}  {pct(trend, n)}")
        print(f"    (b) 터치 통과       {touch:>7}  {pct(touch, trend)}")
        print(f"    (c) 리클레임 통과   {recl:>7}  {pct(recl, touch)}")
        print(f"    장대양봉컷 통과     {final:>7}  {pct(final, recl)}"
              f"   ← 컷에 죽은 신호 {killed}건")
        if recl:
            print(f"    ** 장대양봉컷 학살률 {killed / recl * 100:.1f}% **")

    # ── [3] 앵커 타이밍 ─────────────────────────────────
    print("\n[3] 앵커 타이밍 — 임계값이 적절한가")
    print(f"  {'변형':<14}{'앵커확정':>9}{'앵커시각(중앙)':>16}{'경과봉수 분포':>22}")
    print("  " + "-" * 62)
    for v in variants:
        if v == "session":
            continue
        ago, ts = [], []
        seen = set()
        for r in recs:
            x = r["variants"].get(v, {})
            if x.get("anchor_idx") is None:
                continue
            k = (r["date"], r["code"])
            if k in seen:
                continue
            seen.add(k)
            ago.append(x["anchor_idx"])
            if x.get("anchor_ts"):
                ts.append(str(x["anchor_ts"]))
        if not ago:
            print(f"  {v:<14}{'0':>9}   앵커가 한 번도 안 잡힘 — 임계값이 너무 높음")
            continue
        d = dist(ago)
        med_ts = sorted(ts)[len(ts) // 2] if ts else "—"
        print(f"  {v:<14}{len(ago):>9}{med_ts:>16}"
              f"   p25={d['p25']} 중앙={d['med']} p75={d['p75']} (봉)")
    print("\n  * 경과봉수 = 09:00부터 앵커봉까지의 3분봉 개수. 20 = 10:00 무렵.")
    print("  * 중앙값이 5 미만이면 임계가 낮아 장초반 노이즈를 앵커로 잡는 중.")
    print("  * 중앙값이 60 이상이면 임계가 높아 급등을 놓치고 뒤늦게 잡는 중.")

    # ── [4] 손절갭 ──────────────────────────────────────
    print("\n[4] 손절갭 분포 — 신호 발생 봉 기준")
    print(f"  {'변형':<14}{'n':>6}{'최소':>8}{'p25':>8}{'중앙':>8}"
          f"{'p75':>8}{'p90':>8}{'최대':>8}")
    print("  " + "-" * 62)
    for v in variants:
        gaps = [r["variants"][v]["gap_pct"] for r in recs
                if r["variants"].get(v, {}).get("signal")
                and r["variants"][v].get("gap_pct") is not None]
        d = dist(gaps)
        label = "세션(대조군)" if v == "session" else v
        if not d:
            print(f"  {label:<14}{'0':>6}   신호 없음")
            continue
        print(f"  {label:<14}{d['n']:>6}{d['min']:>8.2f}{d['p25']:>8.2f}"
              f"{d['med']:>8.2f}{d['p75']:>8.2f}{d['p90']:>8.2f}{d['max']:>8.2f}")

    print("\n  기대 범위: 갭 = 회복폭 d + STOP_BUF(1.5%), 장대양봉컷 1.5% →")
    print("  이론상 [1.50%, 3.00%]. 중앙값 위치로 실제 R:R 확정.")
    print("    갭 2.0% → 손익분기 승률 50.0%")
    print("    갭 2.5% → 손익분기 승률 55.6%")
    print("    갭 3.0% → 손익분기 승률 60.0%")

    # ── 요약 ────────────────────────────────────────────
    print("\n" + "=" * W)
    print("  판정 가이드")
    print("=" * W)
    print("  · AVWAP 신호가 대조군의 1.5x 미만이면 → 병목은 VWAP 기준점이 아니다")
    print("  · 장대양봉컷 학살률이 50% 넘으면 → 2번(컷 완화)이 최우선 과제")
    print("  · 앵커 확정율이 70% 미만이면 → 임계값을 낮춰야 함")
    print("  · 갭 중앙값이 2.5% 넘으면 → TP1(2.0%) 상향 검토 필수")
    print("=" * W + "\n")


if __name__ == "__main__":
    main()
