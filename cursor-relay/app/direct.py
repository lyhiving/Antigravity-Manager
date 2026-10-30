"""Cursor AgentService/Run wire format — connect+proto framing + protobuf codec.

Stdlib only, so the framing can be self-checked in CI without installing litellm.

    POST {host}/agent.v1.AgentService/Run
    content-type: application/connect+proto
    body: [flag:1][len:4 BE][payload], payload gzip'd when flag&1
    flag&2 = end-stream (payload is a JSON trailer, still part of a normal run)
"""

from __future__ import annotations

import gzip
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator, Tuple

HOST = os.environ.get("KYA_DIRECT_HOST", "https://agentn.global.api5.cursor.sh").rstrip("/")
PATH = "/agent.v1.AgentService/Run"
ENABLED = os.environ.get("KYA_PROXY_DIRECT", "1").strip() != "0"
# agent 模式默认也走直连：CLI 只提供文本，进程启动白烧 ~4s 首字节。
# 需要 CLI 自己的工具循环(--yolo)时置 KYA_PROXY_DIRECT_AGENT=0。
AGENT = os.environ.get("KYA_PROXY_DIRECT_AGENT", "1").strip() != "0"

# AgentRunRequest.requested_model: "default" = whatever the account resolves to.
MODEL = os.environ.get("KYA_PROXY_DIRECT_MODEL", "default")
# UserMessage.mode enum; 2 is what `cursor-agent --mode ask` sends.
MODE = int(os.environ.get("KYA_PROXY_DIRECT_MODE", "2"))


def client_version() -> str:
    forced = os.environ.get("KYA_CURSOR_CLIENT_VERSION", "").strip()
    if forced:
        return forced
    try:
        root = Path.home() / ".local/share/cursor-agent/versions"
        versions = sorted(p.name for p in root.iterdir() if p.is_dir())
        if versions:
            return f"cli-{versions[-1]}"
    except Exception:
        pass
    return "cli-unknown"


def _pbv(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb_len(field: int, payload: Any) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode()
    return _pbv((field << 3) | 2) + _pbv(len(payload)) + payload


def _pb_int(field: int, n: int) -> bytes:
    return _pbv((field << 3) | 0) + _pbv(n)


def connect_frame(payload: bytes) -> bytes:
    body = gzip.compress(payload)
    return bytes([1]) + len(body).to_bytes(4, "big") + body


def build_frames(prompt: str, conversation_id: str) -> bytes:
    user = _pb_len(1, prompt) + _pb_len(2, str(uuid.uuid4())) + _pb_int(4, MODE)
    query = b"".join(
        [
            _pb_len(1, b""),                        # conversation_state
            # action.user_message_action.user_message — the double nesting is
            # what the CLI actually sends; one level short = "invalid varint".
            _pb_len(2, _pb_len(1, _pb_len(1, user))),
            _pb_len(4, b""),                        # mcp_tools
            _pb_len(5, conversation_id),            # conversation_id
            _pb_len(9, _pb_len(1, MODEL)),          # requested_model
        ]
    )
    # frame 1: AgentClientMessage.run_request
    run_request = _pb_len(1, query)
    # frame 2: the CLI's eager request_context_result; without it the server
    # only sends heartbeats and never starts generating.
    ctx_result = _pb_len(1, _pb_len(1, b""))        # Result.success.request_context
    exec_msg = _pb_len(10, ctx_result) + _pb_int(39, 1)
    return connect_frame(run_request) + connect_frame(_pb_len(2, exec_msg))


def _pb_read_varint(buf: bytes, i: int) -> Tuple[int, int]:
    val = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        val |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return val, i


def pb_fields(buf: bytes) -> list:
    i, out = 0, []
    while i < len(buf):
        key, i = _pb_read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _pb_read_varint(buf, i)
        elif wire == 2:
            size, i = _pb_read_varint(buf, i)
            val = buf[i : i + size]
            i += size
        elif wire == 5:
            val = buf[i : i + 4]
            i += 4
        elif wire == 1:
            val = buf[i : i + 8]
            i += 8
        else:
            raise ValueError(f"bad wire type {wire}")
        out.append((field, wire, val))
    return out


def text_deltas(server_msg: bytes) -> Iterator[str]:
    """AgentServerMessage -> assistant text deltas."""
    for field, wire, value in pb_fields(server_msg):
        if field != 1 or wire != 2:          # 1 = interaction_update
            continue
        for f2, w2, v2 in pb_fields(value):
            if f2 != 1 or w2 != 2:           # 1 = text_delta (4 = thinking_delta)
                continue
            for f3, w3, v3 in pb_fields(v2):
                if f3 == 1 and w3 == 2:
                    yield v3.decode("utf-8", "ignore")


def headers(token: str) -> dict:
    request_id = str(uuid.uuid4())
    trace = "00-" + "0" * 32 + "-" + "0" * 16 + "-00"
    return {
        "authorization": f"Bearer {token}",
        "connect-accept-encoding": "gzip",
        "connect-content-encoding": "gzip",
        "connect-protocol-version": "1",
        "content-type": "application/connect+proto",
        "traceparent": trace,
        "backend-traceparent": trace,
        "user-agent": "connect-es/1.6.1",
        "x-blob-encryption-key": os.urandom(32).hex(),
        "x-cursor-client-type": "cli",
        "x-cursor-client-version": client_version(),
        "x-cursor-timezone": time.strftime("%Z") or "UTC",
        "x-ghost-mode": "false",
        "x-original-request-id": request_id,
        "x-request-id": request_id,
    }


def iter_deltas(chunks: Iterable[bytes]) -> Iterator[str]:
    """Reassemble connect frames from a byte stream and yield text deltas."""
    buf = b""
    for chunk in chunks:
        buf += chunk
        while len(buf) >= 5:
            flag = buf[0]
            size = int.from_bytes(buf[1:5], "big")
            if len(buf) < 5 + size:
                break
            msg = buf[5 : 5 + size]
            buf = buf[5 + size :]
            if flag & 2:
                return
            if flag & 1:
                try:
                    msg = gzip.decompress(msg)
                except Exception:
                    continue
            try:
                yield from text_deltas(msg)
            except Exception:
                continue
