#!/usr/bin/env python3
"""Card-pool credential manager for the Linux Cursor relay.

Replaces the two macOS-bound helpers in relay.py that talked to the
KeepingYouAwake app; everything else in relay.py is platform-neutral.

  relay.trigger_quota_switch     -> start a renewal worker
  relay.wait_for_quota_renewal   -> poll the state file it writes

sync_auth_from_active_token / _read_active_token are NOT replaced: they only
touch Path.home()/.cursor-tk and Path.home()/.cursor, identical on Linux.

Renewal contract (verified from KYACursorRenewalManager.m):
  POST {host}/hou/dwd/m3qr/r1   {"cardCode","deviceId"}
       -> {success, token, limitReached, message}
  POST {host}/hou/dwd/m3qr/rs1  {"cardCode","deviceId"}
       reset: -1 day of card time, +60 switches, then /r1 again
  hosts: www.skhxd.top, then www.xxdlzs.cn
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path

DATA_ROOT = Path(
    os.environ.get("ABV_DATA_DIR") or (Path.home() / ".antigravity_tools")
) / "cursor"
CARDS_FILE = DATA_ROOT / "cards.json"
HISTORY_FILE = DATA_ROOT / "renewal_history.json"
HISTORY_LIMIT = 200
SNAPSHOT_HISTORY = 20

TK_DIR = Path.home() / ".cursor-tk"
ACTIVE_TOKEN = TK_DIR / "active_token"
STATE_FILE = TK_DIR / "kya_switch_state.json"
# 放在 ABV_DATA_DIR 下（已挂载持久卷），否则重置后的指纹会随容器重建丢失并退回默认值
DEVICE_ID_FILE = DATA_ROOT / "device_id.txt"

DEFAULT_DEVICE_ID = "D6CDA11D-E07A-5B09-BE57-B3AD55284470"
HOSTS = ("https://www.skhxd.top", "https://www.xxdlzs.cn")
R1_PATH = "/hou/dwd/m3qr/r1"
RS1_PATH = "/hou/dwd/m3qr/rs1"

WAIT_TIMEOUT_S = float(os.environ.get("CURSOR_RENEW_WAIT_S", "120"))
HTTP_TIMEOUT_S = float(os.environ.get("CURSOR_RENEW_HTTP_S", "20"))

_guard = threading.Lock()
_running = False


# ---------------------------------------------------------------- fs helpers
def _read_json(path: Path, default):
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_state() -> dict:
    st = _read_json(STATE_FILE, {})
    return st if isinstance(st, dict) else {}


def write_state(status: str, detail: str = "") -> None:
    _write_json(
        STATE_FILE,
        {
            "status": status,
            "message": detail,
            "timestamp": int(time.time() * 1000),
        },
    )


def load_pool() -> dict:
    raw = _read_json(CARDS_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    cards = [
        c
        for c in (raw.get("cards") or [])
        if isinstance(c, dict) and str(c.get("cardCode") or "").strip()
    ]
    return {"active": int(raw.get("active") or 0), "cards": cards}


def save_pool(pool: dict) -> None:
    _write_json(CARDS_FILE, pool)


def device_id() -> str:
    try:
        if DEVICE_ID_FILE.is_file():
            value = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
            if value:
                return value
    except Exception:
        pass
    return DEFAULT_DEVICE_ID


def reset_device_id() -> tuple[str, str]:
    """Regenerate the fingerprint sent with card activation (KYA 重置机器码 等价操作).

    Linux 侧没有 Cursor 客户端可改写，唯一被激活接口读取的指纹就是
    device_id.txt，所以这里生成全新 UUID、备份旧值后落盘。返回 (new, old)。
    """
    try:
        old = DEVICE_ID_FILE.read_text(encoding="utf-8").strip() if DEVICE_ID_FILE.is_file() else ""
    except Exception:
        old = ""
    if old:
        backup = DEVICE_ID_FILE.with_name(
            f"device_id.txt.bak.{time.strftime('%Y%m%d_%H%M%S')}"
        )
        try:
            backup.write_text(old, encoding="utf-8")
        except Exception:
            pass
    new = str(uuid.uuid4()).upper()
    DEVICE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEVICE_ID_FILE.write_text(new, encoding="utf-8")
    return new, old


def load_history() -> list:
    raw = _read_json(HISTORY_FILE, [])
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def add_history(entry: dict) -> None:
    items = load_history()
    items.insert(0, entry)
    _write_json(HISTORY_FILE, items[:HISTORY_LIMIT])


def read_active_token() -> str:
    try:
        if ACTIVE_TOKEN.is_file():
            return ACTIVE_TOKEN.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        pass
    return ""


def write_active_token(token: str) -> None:
    ACTIVE_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_TOKEN.write_text(token, encoding="utf-8")


# --------------------------------------------------------------- renewal api
def _post(path: str, card_code: str) -> dict:
    body = json.dumps(
        {"cardCode": card_code, "deviceId": device_id()}
    ).encode("utf-8")
    last_error = ""
    for host in HOSTS:
        try:
            req = urllib.request.Request(
                host + path,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                payload = resp.read().decode("utf-8", "ignore").strip() or "{}"
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return parsed
            last_error = "non-object response"
        except Exception as exc:  # network error or bad JSON -> try next host
            last_error = f"{type(exc).__name__}: {exc}"
    return {"success": False, "message": f"所有服务节点均无法连接 ({last_error})"}


def _token_of(res: dict) -> str:
    return str(res.get("token") or "").strip()


def renew_card(card: dict) -> tuple[bool, str, str]:
    """Renew one card. Returns (ok, token, message)."""
    code = str(card.get("cardCode") or "")

    res = _post(R1_PATH, code)
    token = _token_of(res)
    if len(token) > 20:
        return True, token, ""

    if res.get("limitReached"):
        rs = _post(RS1_PATH, code)
        token = _token_of(rs)
        if len(token) > 20:
            return True, token, "reset"
        if rs.get("success"):
            res = _post(R1_PATH, code)
            token = _token_of(res)
            if len(token) > 20:
                return True, token, "reset+renew"
            return False, "", str(res.get("message") or "充能后换杯失败")
        return False, "", str(rs.get("message") or "充能重置失败")

    return False, "", str(res.get("message") or "换杯失败")


def renew_next_card() -> tuple[bool, str, str]:
    """Walk the pool from the active card; first success wins.

    Per policy: a failed key must not end the session — try the whole pool,
    and only surface failure once every key has been tried.
    """
    pool = load_pool()
    cards = pool["cards"]
    if not cards:
        return False, "", "卡池为空，请先在管理页添加卡密"

    start = pool.get("active", 0) % len(cards)
    errors = []
    for offset in range(len(cards)):
        idx = (start + offset) % len(cards)
        card = cards[idx]
        label = str(card.get("email") or f"#{idx}")
        try:
            ok, token, msg = renew_card(card)
        except Exception as exc:
            ok, token, msg = False, "", f"{type(exc).__name__}: {exc}"
        if ok:
            card["lastRenewAt"] = int(time.time())
            card["renewCount"] = int(card.get("renewCount") or 0) + 1
            pool["active"] = idx
            save_pool(pool)
            write_active_token(token)
            return True, token, label
        errors.append(f"{label}: {msg}")
    return False, "", " | ".join(errors)


# ------------------------------------------------- relay-compatible surface
def _worker() -> None:
    global _running
    try:
        ok, _token, detail = renew_next_card()
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    add_history(
        {
            "at": int(time.time()),
            "ok": ok,
            "card": detail if ok else "",
            "message": "" if ok else detail,
            "deviceId": device_id(),
        }
    )
    write_state("success" if ok else "error", detail)
    print(
        f"[cursor-relay] renewal {'ok' if ok else 'failed'}: {detail}",
        flush=True,
    )
    with _guard:
        _running = False


def trigger_quota_switch(session_name: str = "cursor-relay") -> None:
    """Kick off a renewal in the background (mirrors the KYA signal file)."""
    global _running
    with _guard:
        if _running:
            print("[cursor-relay] renewal already in progress, coalescing", flush=True)
            return
        _running = True
    write_state("switching", session_name)
    threading.Thread(target=_worker, name="cursor-renewal", daemon=True).start()


def wait_for_quota_renewal(
    since_ms: float, timeout_s: float = WAIT_TIMEOUT_S
) -> bool:
    """Poll the renewal state file until it settles or times out."""
    since_floor = since_ms - 1000
    deadline = time.time() + timeout_s
    last_log = 0.0
    while time.time() < deadline:
        st = read_state()
        if float(st.get("timestamp") or 0) >= since_floor:
            status = st.get("status")
            if status == "success":
                print(f"[cursor-relay] renewal ok ({st.get('message')})", flush=True)
                return True
            if status == "error":
                print(
                    f"[cursor-relay] renewal failed: {st.get('message')}", flush=True
                )
                return False
        now = time.time()
        if now - last_log >= 15:
            last_log = now
            print(f"[cursor-relay] waiting for renewal… ({int(timeout_s - (deadline - now))}s)", flush=True)
        time.sleep(0.5)
    print("[cursor-relay] renewal wait timed out", flush=True)
    return False


# ------------------------------------------------------------ admin helpers
def mask(token: str) -> str:
    if not token:
        return ""
    if len(token) < 20:
        return "*" * len(token)
    return f"{token[:8]}…{token[-6:]}"


def snapshot() -> dict:
    pool = load_pool()
    cards = []
    for idx, card in enumerate(pool["cards"]):
        cards.append(
            {
                "index": idx,
                "email": card.get("email") or "",
                "cardCode": mask(str(card.get("cardCode") or "")),
                "cardType": card.get("cardType") or "月卡",
                "startTime": card.get("startTime") or "",
                "endTime": card.get("endTime") or "",
                "renewCount": int(card.get("renewCount") or 0),
                "lastRenewAt": card.get("lastRenewAt") or 0,
                "active": idx == pool.get("active", 0),
            }
        )
    return {
        "cards": cards,
        "activeIndex": pool.get("active", 0),
        "activeToken": mask(read_active_token()),
        "deviceId": device_id(),
        "state": read_state(),
        "renewing": _running,
        "history": load_history()[:SNAPSHOT_HISTORY],
    }
