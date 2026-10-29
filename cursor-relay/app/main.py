#!/usr/bin/env python3
"""Cursor relay sidecar: vendored KYA proxy + Linux credential layer + admin UI."""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

import uvicorn
from fastapi import Depends, Header, HTTPException
from fastapi.responses import HTMLResponse

import pool
import relay

# Only these two are macOS-bound; the rest of relay.py already runs on Linux.
relay.trigger_quota_switch = pool.trigger_quota_switch
relay.wait_for_quota_renewal = pool.wait_for_quota_renewal

app = relay.app
ADMIN_PASSWORD = os.environ.get("CURSOR_ADMIN_PASSWORD", "")
ADMIN_HTML = (Path(__file__).with_name("admin.html")).read_text(encoding="utf-8")
HISTORY_HTML = (Path(__file__).with_name("history.html")).read_text(encoding="utf-8")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="authentication required",
        headers={"WWW-Authenticate": 'Basic realm="cursor-relay"'},
    )


def admin_auth(authorization: str | None = Header(default=None)) -> None:
    """Basic(独立 admin 页) 或 Bearer(内嵌到 Manager 控制台的 tab) 任一都放行。"""
    if not ADMIN_PASSWORD:
        return
    if not authorization:
        raise _unauthorized()
    scheme, _, credential = authorization.partition(" ")
    scheme = scheme.lower()

    if scheme == "bearer":
        if secrets.compare_digest(credential.strip(), ADMIN_PASSWORD):
            return
        raise _unauthorized()
    if scheme != "basic":
        raise _unauthorized()

    try:
        decoded = base64.b64decode(credential).decode("utf-8")
    except Exception:
        raise _unauthorized()
    _user, _, password = decoded.partition(":")
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        raise _unauthorized()


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(admin_auth)])
async def admin_page() -> str:
    return ADMIN_HTML


@app.get("/admin/history", response_class=HTMLResponse, dependencies=[Depends(admin_auth)])
async def history_page() -> str:
    return HISTORY_HTML


@app.get("/admin/api/state", dependencies=[Depends(admin_auth)])
async def admin_state() -> dict:
    return pool.snapshot()


@app.get("/admin/api/history", dependencies=[Depends(admin_auth)])
async def admin_history(limit: int = 50, offset: int = 0) -> dict:
    items = pool.load_history()
    offset = max(0, offset)
    limit = min(max(1, limit), pool.HISTORY_LIMIT)
    return {
        "total": len(items),
        "offset": offset,
        "items": items[offset:offset + limit],
    }


@app.post("/admin/api/cards", dependencies=[Depends(admin_auth)])
async def add_card(payload: dict) -> dict:
    code = str(payload.get("cardCode") or "").strip()
    if len(code) < 6:
        raise HTTPException(status_code=400, detail="cardCode 无效")
    data = pool.load_pool()
    if any(c.get("cardCode") == code for c in data["cards"]):
        raise HTTPException(status_code=409, detail="卡密已存在")
    data["cards"].append(
        {
            "email": str(payload.get("email") or "").strip(),
            "cardCode": code,
            "cardType": str(payload.get("cardType") or "月卡").strip(),
            "startTime": str(payload.get("startTime") or "").strip(),
            "endTime": str(payload.get("endTime") or "").strip(),
            "renewCount": 0,
            "lastRenewAt": 0,
        }
    )
    pool.save_pool(data)
    return pool.snapshot()


@app.post("/admin/api/cards/delete", dependencies=[Depends(admin_auth)])
async def delete_card(payload: dict) -> dict:
    data = pool.load_pool()
    try:
        idx = int(payload.get("index"))
        data["cards"].pop(idx)
    except Exception:
        raise HTTPException(status_code=400, detail="index 无效")
    if data["cards"]:
        data["active"] = min(max(int(data.get("active") or 0), 0), len(data["cards"]) - 1)
    else:
        data["active"] = 0
    pool.save_pool(data)
    return pool.snapshot()


@app.post("/admin/api/active", dependencies=[Depends(admin_auth)])
async def set_active(payload: dict) -> dict:
    data = pool.load_pool()
    try:
        idx = int(payload.get("index"))
    except Exception:
        raise HTTPException(status_code=400, detail="index 无效")
    if not (0 <= idx < len(data["cards"])):
        raise HTTPException(status_code=400, detail="index 越界")
    data["active"] = idx
    pool.save_pool(data)
    return pool.snapshot()


@app.post("/admin/api/renew", dependencies=[Depends(admin_auth)])
async def renew_now() -> dict:
    pool.trigger_quota_switch("cursor-admin")
    return pool.snapshot()


@app.post("/admin/api/auto-renew", dependencies=[Depends(admin_auth)])
async def set_auto_renew(payload: dict) -> dict:
    pool.set_auto_renew(bool(payload.get("enabled")))
    return pool.snapshot()


@app.post("/admin/api/device/reset", dependencies=[Depends(admin_auth)])
async def reset_device() -> dict:
    if pool.snapshot().get("renewing"):
        raise HTTPException(status_code=409, detail="续杯中，请稍后再重置设备 ID")
    new, _old = pool.reset_device_id()
    snapshot = pool.snapshot()
    snapshot["state"] = {"status": "success", "message": f"设备 ID 已重置为 {new}"}
    return snapshot


def main() -> None:
    host = os.environ.get("KYA_PROXY_HOST", "0.0.0.0")
    port = int(os.environ.get("KYA_PROXY_PORT", "8317"))
    print(f"[cursor-relay] listening on http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
