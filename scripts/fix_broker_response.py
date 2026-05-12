"""broker_response 마이그레이션: str() → json.dumps() 형식 변환.

기존 레코드: {'dry_run': True, ...}  (Python repr, json.loads 불가)
변환 후:    {"dry_run": true, ...}   (JSON, json.loads 가능)

실행: docker exec -w /app <container> python scripts/fix_broker_response.py
"""
import ast
import json
from sqlalchemy import create_engine, text

DB_PATH = "/app/data/trades.db"
engine = create_engine(f"sqlite:///{DB_PATH}")

with engine.begin() as conn:
    rows = conn.execute(text("SELECT id, broker_response FROM trade_log")).fetchall()
    updated = 0
    skipped = 0
    for row_id, br in rows:
        if not br:
            continue
        # 이미 valid JSON이면 스킵
        try:
            json.loads(br)
            skipped += 1
            continue
        except (json.JSONDecodeError, ValueError):
            pass
        # Python repr → ast.literal_eval → json.dumps
        try:
            obj = ast.literal_eval(br)
            new_br = json.dumps(obj, ensure_ascii=False)
            conn.execute(
                text("UPDATE trade_log SET broker_response = :br WHERE id = :id"),
                {"br": new_br[:512], "id": row_id},
            )
            updated += 1
        except Exception as e:
            print(f"  id={row_id} 변환 실패: {e!r} | raw={br[:80]!r}")

print(f"완료: {updated}건 변환, {skipped}건 스킵 (이미 JSON)")
