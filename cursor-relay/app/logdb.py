"""Append proxy requests to Antigravity Manager's shared `request_logs` table.

The relay container mounts the same ABV_DATA_DIR as the manager, so the row
lands in the same SQLite file the Monitor page reads. WAL + busy_timeout make
concurrent writes from both processes safe; any failure here is swallowed so
the proxy path never pays for logging.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(
    os.environ.get("ABV_DATA_DIR") or (Path.home() / ".antigravity_tools")
) / "proxy_logs.db"

# 列名与 Rust 侧 save_log 的 INSERT 一致
_COLUMNS = (
    "id", "timestamp", "method", "url", "status", "duration",
    "model", "mapped_model", "upstream_model", "account_email",
    "protocol", "username", "client_ip", "error", "response_headers",
    "session_id", "input_tokens", "output_tokens", "cached_tokens",
    "request_body", "upstream_request_body", "response_body",
)

# Monitor 明细页一次性渲染整段报文，超长会把页面拖死，入库存截断版
BODY_LIMIT = 256 * 1024


def _cap(text: str | None) -> str | None:
    if not text:
        return None
    if len(text) <= BODY_LIMIT:
        return text
    return f"{text[:BODY_LIMIT]}\n…[truncated {len(text) - BODY_LIMIT} chars]"


def _pretty(text: str | None) -> str | None:
    """能整段解析成 JSON 就缩进展开，跟 Rust 侧响应报文观感一致。"""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except Exception:
        return text


def _mask_account(account: str) -> str:
    """入库即脱敏：邮箱留前 3 位 + 域名（与 Monitor 前端规则一致），卡密只留前 3 位。"""
    if "@" in account:
        local, _, domain = account.partition("@")
        return f"{local[:3]}***@{domain}"
    return f"{account[:3]}***"


def _active_email() -> str | None:
    """当前卡池里支撑这条请求的账号（pool.py 的 active 卡），脱敏后返回。"""
    try:
        import pool

        data = pool.load_pool()
        card = data["cards"][int(data.get("active") or 0)]
        raw = str(card.get("email") or "").strip()
        return _mask_account(raw) if raw else None
    except Exception:
        return None


def record(
    *,
    method: str,
    url: str,
    status: int,
    duration_ms: int,
    model: str | None = None,
    protocol: str | None = None,
    client_ip: str | None = None,
    error: str | None = None,
    ttft_ms: int | None = None,
    session_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    request_body: str | None = None,
    upstream_request_body: str | None = None,
    response_body: str | None = None,
) -> None:
    if not DB_PATH.exists():
        return
    row = (
        uuid.uuid4().hex,
        int(time.time() * 1000),
        method,
        url,
        status,
        duration_ms,
        model,
        None,
        None,
        _active_email(),
        protocol,
        "cursor-relay",
        client_ip,
        error[:1024] if error else None,
        # Monitor 页的「首字」从 response_headers 里读 x-timing-ttft-ms（同 Rust 侧约定）
        json.dumps({"x-timing-ttft-ms": str(ttft_ms)}) if ttft_ms is not None else None,
        session_id,
        input_tokens,
        output_tokens,
        cached_tokens,
        _cap(_pretty(request_body)),
        _cap(upstream_request_body),
        _cap(response_body),
    )
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2.5)
        try:
            conn.execute(
                f"INSERT INTO request_logs ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_COLUMNS))})",
                row,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[cursor-relay] request log skipped: {e}", flush=True)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        DB_PATH = Path(d) / "proxy_logs.db"
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "CREATE TABLE request_logs (id TEXT PRIMARY KEY, timestamp INTEGER, "
            "method TEXT, url TEXT, status INTEGER, duration INTEGER, model TEXT, "
            "mapped_model TEXT, upstream_model TEXT, account_email TEXT, protocol TEXT, "
            "username TEXT, client_ip TEXT, error TEXT, response_headers TEXT, "
            "session_id TEXT, input_tokens INTEGER, output_tokens INTEGER, "
            "cached_tokens INTEGER, request_body TEXT, upstream_request_body TEXT, "
            "response_body TEXT)"
        )
        conn.commit()
        conn.close()

        before = int(time.time() * 1000)
        record(
            method="POST",
            url="/v1/messages",
            status=200,
            duration_ms=1234,
            model="cursor-local",
            protocol="anthropic",
            client_ip="127.0.0.1",
            ttft_ms=456,
            session_id="sess-1",
            input_tokens=99,
            output_tokens=40,
            cached_tokens=9344,
            request_body='{"model":"cursor-local","messages":[]}',
            upstream_request_body="P" * (BODY_LIMIT + 50),
            response_body='data: {"delta":{"text":"hi"}}\n\n',
        )
        record(method="POST", url="/v1/messages", status=502,
               duration_ms=7, error="X" * 4000)

        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT timestamp, status, duration, model, protocol, username, "
            "client_ip, length(error), response_headers, session_id, "
            "input_tokens, output_tokens, cached_tokens, request_body, "
            "upstream_request_body, response_body "
            "FROM request_logs ORDER BY timestamp"
        ).fetchall()
        conn.close()

        ts, status, dur, model, proto, user, ip, errlen, rh = rows[0][:9]
        assert ts >= before and status == 200 and dur == 1234, rows[0]
        assert model == "cursor-local" and proto == "anthropic" and user == "cursor-relay"
        assert ip == "127.0.0.1" and errlen is None, rows[0]
        assert json.loads(rh)["x-timing-ttft-ms"] == "456", rh
        assert rows[0][9:13] == ("sess-1", 99, 40, 9344), rows[0]
        assert rows[1][1] == 502 and rows[1][7] == 1024, "error 截断 1024"
        assert rows[1][8] is None, "无首字时 response_headers 留空"
        assert rows[1][10:] == (None, None, None, None, None, None), "无 usage 时留空"

        # 报文：请求体缩进展开，超长转出报文截断，流式响应保留原文
        assert rows[0][13] == '{\n  "model": "cursor-local",\n  "messages": []\n}', rows[0][13]
        assert rows[0][14].startswith("P" * 100) and "[truncated 50 chars]" in rows[0][14]
        assert rows[0][15].startswith("data: "), rows[0][15]

        # 账号脱敏：邮箱形态与前端同规则，卡密只留前 3 位
        assert _mask_account("hermes.chen@gmail.com") == "her***@gmail.com"
        assert _mask_account("GGLY8T8OPN") == "GGL***"
        assert _mask_account("") == "***"
        print("ok  logdb self-check")
