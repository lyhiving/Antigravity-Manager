"""Append proxy requests to Antigravity Manager's shared `request_logs` table.

The relay container mounts the same ABV_DATA_DIR as the manager, so the row
lands in the same SQLite file the Monitor page reads. WAL + busy_timeout make
concurrent writes from both processes safe; any failure here is swallowed so
the proxy path never pays for logging.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(
    os.environ.get("ABV_DATA_DIR") or (Path.home() / ".antigravity_tools")
) / "proxy_logs.db"

# 列名与 Rust 侧 save_log 的 INSERT 一致；body 类字段留空，避免重复占盘
_COLUMNS = (
    "id", "timestamp", "method", "url", "status", "duration",
    "model", "mapped_model", "upstream_model", "account_email",
    "protocol", "username", "client_ip", "error", "session_id",
)


def _active_email() -> str | None:
    """当前卡池里支撑这条请求的卡密邮箱（pool.py 的 active 卡）。"""
    try:
        import pool

        data = pool.load_pool()
        card = data["cards"][int(data.get("active") or 0)]
        return card.get("email") or None
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
        None,
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
            "username TEXT, client_ip TEXT, error TEXT, session_id TEXT)"
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
        )
        record(method="POST", url="/v1/messages", status=502,
               duration_ms=7, error="X" * 4000)

        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT timestamp, status, duration, model, protocol, username, "
            "client_ip, length(error) FROM request_logs ORDER BY timestamp"
        ).fetchall()
        conn.close()

        ts, status, dur, model, proto, user, ip, errlen = rows[0]
        assert ts >= before and status == 200 and dur == 1234, rows[0]
        assert model == "cursor-local" and proto == "anthropic" and user == "cursor-relay"
        assert ip == "127.0.0.1" and errlen is None, rows[0]
        assert rows[1][1] == 502 and rows[1][7] == 1024, "error 截断 1024"
        print("ok  logdb self-check")
