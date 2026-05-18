"""사용자 웹 UI 저장 파라미터 (스마트 머지 적용).

설계:
- 웹 UI 저장 → data/user_params.json 에만 기록 (.env.overrides 안 건드림)
- 동시에 base_at_save 도 기록 (저장 시점의 .env.overrides 값)
- 봇 동작 시: .env + .env.overrides 기본값으로 읽고, user_params 의 키만 덮어씀
- 단, .env.overrides 의 현재 값이 base_at_save 와 다르면 → PC가 변경 → user_params 에서 제거

이렇게 하면:
- PC 가 다른 키 또는 같은 키를 안 건드리면 → 웹 값 유지
- PC 가 같은 키를 명시적으로 변경했으면 → PC 값 적용 + user_params 정리
- git pull 충돌 없음 (.env.overrides 는 항상 git 만 수정)
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Iterable


def _project_root() -> Path:
    # stock_bot/config/user_params.py → parents[2] = project root
    return Path(__file__).resolve().parents[2]


def user_params_path() -> Path:
    return _project_root() / "data" / "user_params.json"


def load_user_params() -> dict[str, dict]:
    p = user_params_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # 정상화: 구조 {"key": {"value": ..., "base_at_save": ...}}
        out: dict[str, dict] = {}
        for k, v in data.items():
            if isinstance(v, dict) and "value" in v:
                out[k] = {"value": str(v["value"]), "base_at_save": str(v.get("base_at_save", ""))}
            else:
                # 하위호환: 단순 {"key": "value"} 형식이면 base_at_save 비움
                out[k] = {"value": str(v), "base_at_save": ""}
        return out
    except Exception:
        return {}


def save_user_params(data: dict[str, dict]) -> None:
    p = user_params_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_user_save(key: str, value: str, base_at_save: str) -> None:
    """웹 UI 저장 시 user_params 갱신."""
    data = load_user_params()
    data[key] = {"value": str(value), "base_at_save": str(base_at_save)}
    save_user_params(data)


def remove_user_param(key: str) -> bool:
    """user_params 에서 특정 키 제거 (수동 리셋 또는 자동 만료)."""
    data = load_user_params()
    if key in data:
        del data[key]
        save_user_params(data)
        return True
    return False


def apply_smart_merge(parsed_env: dict[str, str]) -> dict[str, str]:
    """parsed_env (.env + .env.overrides) 위에 user_params 를 스마트 머지.

    파라미터:
        parsed_env: {"KEY": "VALUE"} - .env 와 .env.overrides 를 이미 합친 dict

    반환: 같은 형식의 새 dict (user_params 적용 후)

    Side effect: user_params.json 의 stale entry (base_at_save != 현재값) 자동 정리.
    """
    user_data = load_user_params()
    if not user_data:
        return parsed_env

    result = dict(parsed_env)
    changed = False
    to_remove: list[str] = []

    for key, info in user_data.items():
        current_in_file = parsed_env.get(key, "")
        base_at_save   = info.get("base_at_save", "")
        user_value     = info.get("value", "")

        if base_at_save and current_in_file != base_at_save:
            # PC 가 .env.overrides 의 이 키를 변경함 → 사용자 오버라이드 만료
            to_remove.append(key)
            # result 에는 current_in_file 값 그대로 (이미 들어있음)
        else:
            # PC 가 안 건드림 → 사용자 값 적용
            result[key] = user_value

    if to_remove:
        for k in to_remove:
            del user_data[k]
        save_user_params(user_data)
        changed = True

    return result


def get_user_params_summary() -> dict:
    """디버그/UI 표시용 — 현재 사용자 오버라이드 키 목록."""
    return {k: info.get("value", "") for k, info in load_user_params().items()}
