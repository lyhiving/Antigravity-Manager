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
# 与 KYA 保持一致：缺 UA 会被站点 WAF 直接 403
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

WAIT_TIMEOUT_S = float(os.environ.get("CURSOR_RENEW_WAIT_S", "120"))
# KYA 的 timeoutInterval 是 6.0
HTTP_TIMEOUT_S = float(os.environ.get("CURSOR_RENEW_HTTP_S", "6"))
# KYA cooldownDurationSeconds 官方值 30s（旧版 180 已废弃）
COOLDOWN_S = float(os.environ.get("CURSOR_RENEW_COOLDOWN_S", "30"))
# KYA 首次失败后的静默退避重试
RETRY_SLEEP_S = 1.0
DAILY_FILE = DATA_ROOT / "daily_switch.json"
# 自动续杯开关：文件缺省视为关闭（只有管理页显式打开才生效）
AUTO_RENEW_FILE = DATA_ROOT / "auto_renew.json"

_guard = threading.Lock()
_running = False
_last_switch_at = 0.0


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


def write_state(status: str, detail: str = "", phase: str = "") -> None:
    _write_json(
        STATE_FILE,
        {
            "status": status,
            "phase": phase,
            "message": detail,
            "timestamp": int(time.time() * 1000),
            "pid": os.getpid(),
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


def load_daily() -> dict:
    raw = _read_json(DAILY_FILE, {})
    today = time.strftime("%Y-%m-%d")
    if not isinstance(raw, dict) or raw.get("date") != today:
        return {"date": today, "switches": 0, "resets": 0}
    return raw


def bump_daily(field: str, delta: int = 1) -> dict:
    daily = load_daily()
    daily[field] = max(0, int(daily.get(field) or 0) + delta)
    _write_json(DAILY_FILE, daily)
    return daily


def effective_daily_total(daily: dict) -> int:
    """KYA: (max(重置次数, 换号次数//60) + 1) * 60 —— 每次充能重置 +60 次换号额度。"""
    resets = max(int(daily.get("resets") or 0), int(daily.get("switches") or 0) // 60)
    return (resets + 1) * 60


def auto_renew_enabled() -> bool:
    raw = _read_json(AUTO_RENEW_FILE, {})
    return isinstance(raw, dict) and bool(raw.get("enabled"))


def set_auto_renew(enabled: bool) -> bool:
    _write_json(AUTO_RENEW_FILE, {"enabled": bool(enabled), "updatedAt": int(time.time())})
    return bool(enabled)


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
    """与 KYA requestRenewalApiWithCardCode 一致：失败才走容灾备用节点。"""
    body = json.dumps(
        {"cardCode": card_code, "deviceId": device_id()}
    ).encode("utf-8")
    last: dict | None = None
    last_error = ""
    for host in HOSTS:
        try:
            req = urllib.request.Request(
                host + path,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                payload = resp.read().decode("utf-8", "ignore").strip() or "{}"
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                last_error = "non-object response"
                continue
            last = parsed
            # 成功 / 已含 token / 额度已满 => 直接返回，不再尝试备用节点
            if (
                parsed.get("success")
                or len(_token_of(parsed)) > 20
                or parsed.get("limitReached")
            ):
                return parsed
        except Exception as exc:  # network error or bad JSON -> try next host
            last_error = f"{type(exc).__name__}: {exc}"
    if last is not None:
        return last
    return {"success": False, "message": f"所有服务节点均无法连接 ({last_error})"}


def _token_of(res: dict) -> str:
    return str(res.get("token") or "").strip()


def _ok(res: dict) -> bool:
    """KYA: success && token.length > 20 才算换号成功。"""
    return bool(res.get("success")) and len(_token_of(res)) > 20


def renew_card(card: dict) -> tuple[bool, str, str]:
    """Renew one card. Returns (ok, token, message)."""
    code = str(card.get("cardCode") or "")

    res = _post(R1_PATH, code)
    if res.get("limitReached"):
        write_state(
            "resetting",
            "卡密今日额度已满，正在自动扣时长充能重置额度(+60次)...",
            "requesting_rs1",
        )
        rs = _post(RS1_PATH, code)
        if _ok(rs):
            bump_daily("resets")
            return True, _token_of(rs), "reset"
        # KYA: 扣时长重置未通过 -> 直接轮换备用卡密，不再重试本卡
        return False, "", str(rs.get("message") or "扣时长重置未通过")

    if _ok(res):
        return True, _token_of(res), ""

    # KYA: 首次未成功 -> 1s 静默退避重试
    time.sleep(RETRY_SLEEP_S)
    res = _post(R1_PATH, code)
    if _ok(res):
        return True, _token_of(res), ""
    return False, "", str(res.get("message") or "云端 API 换号未返回有效凭据")


def renew_next_card() -> tuple[bool, str, str]:
    """Walk the pool from the active card; first success wins.

    与 KYA rotateToNextCard 一致：只向后轮换、不绕回池首；池尾仍失败才报错。
    """
    pool = load_pool()
    cards = pool["cards"]
    if not cards:
        return False, "", "卡池为空，请先在管理页添加卡密"

    start = pool.get("active", 0) % len(cards)
    errors = []
    for idx in range(start, len(cards)):
        card = cards[idx]
        label = str(card.get("email") or f"#{idx}")
        try:
            ok, token, msg = renew_card(card)
        except Exception as exc:
            ok, token, msg = False, "", f"{type(exc).__name__}: {exc}"
        if ok:
            card["lastRenewAt"] = int(time.time())
            card["renewCount"] = int(card.get("renewCount") or 0) + 1
            if idx != start:
                # KYA: 轮换到备用卡密后今日换号计数清零
                bump_daily("switches", -10**9)
            pool["active"] = idx
            save_pool(pool)
            write_active_token(token)
            return True, token, label
        errors.append(f"{label}: {msg}")
    return False, "", " | ".join(errors)


# ------------------------------------------------- relay-compatible surface
def _worker(reason: str = "manual") -> None:
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
            "reason": reason,
        }
    )
    write_state(
        "success" if ok else "error",
        "极速换号完成" if ok else detail,
        "completed" if ok else "failed",
    )
    print(
        f"[cursor-relay] renewal {'ok' if ok else 'failed'} ({reason}): {detail}",
        flush=True,
    )
    with _guard:
        _running = False


def trigger_quota_switch(session_name: str = "cursor-relay") -> None:
    """KYA executeSwitchWithReason：防封冷却门控 -> 计数 -> 异步换号。"""
    global _running, _last_switch_at
    # reason: 控制台按钮触发=manual，聊天撞到额度上限自动换号=auto（KYA 的 Quota 类原因）
    reason = "manual" if session_name == "cursor-admin" else "auto"

    # 自动续杯默认关闭：只有管理页开关打开后才允许聊天撞额度时自动换号
    if reason == "auto" and not auto_renew_enabled():
        detail = "自动续杯已关闭，跳过换号"
        print(f"[cursor-relay] {detail}", flush=True)
        write_state("skipped", detail, "auto_disabled")
        return

    now = time.time()
    if _last_switch_at > 0 and (now - _last_switch_at) < COOLDOWN_S:
        remain = int(COOLDOWN_S - (now - _last_switch_at))
        if reason == "auto":
            # KYA: Quota/耗尽 类原因排队，冷却结束后自动重入
            print(
                f"[cursor-relay] ⏳ 遇到防封冷却（还需 {remain} 秒），已排队等待冷却后自动执行换号...",
                flush=True,
            )
            timer = threading.Timer(remain + 1, trigger_quota_switch, (session_name,))
            timer.daemon = True
            timer.start()
            return
        # KYA: 手动触发在冷却期内直接跳过
        detail = f"防封冷却中（还需等待 {remain} 秒），跳过换号触发"
        print(f"[cursor-relay] ⏳ {detail}", flush=True)
        write_state("skipped", detail, "cooldown")
        return

    with _guard:
        if _running:
            print("[cursor-relay] renewal already in progress, coalescing", flush=True)
            return
        _running = True
    _last_switch_at = now
    # KYA: 换号额度由云端权威控制，本地仅计数展示
    daily = bump_daily("switches")
    write_state("switching", "正在向云端申请新账号与凭据...", "requesting_api")
    print(
        f"[cursor-relay] 🚀 执行换号 [{reason}] (今日第 {daily['switches']}/"
        f"{effective_daily_total(daily)} 次)",
        flush=True,
    )
    threading.Thread(
        target=_worker, args=(reason,), name="cursor-renewal", daemon=True
    ).start()


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
    daily = load_daily()
    switches = int(daily.get("switches") or 0)
    total = effective_daily_total(daily)
    return {
        "cards": cards,
        "activeIndex": pool.get("active", 0),
        "activeToken": mask(read_active_token()),
        "deviceId": device_id(),
        "state": read_state(),
        "autoRenew": auto_renew_enabled(),
        "renewing": _running,
        "daily": {
            "date": daily["date"],
            "switches": switches,
            "resets": int(daily.get("resets") or 0),
            "total": total,
            "remaining": max(0, total - switches),
            "cooldownRemaining": max(
                0, int(COOLDOWN_S - (time.time() - _last_switch_at))
            )
            if _last_switch_at > 0
            else 0,
        },
        "history": load_history()[:SNAPSHOT_HISTORY],
    }
