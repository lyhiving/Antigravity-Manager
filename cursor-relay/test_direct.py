#!/usr/bin/env python3
"""Self-check for the AgentService/Run wire format in app/direct.py (stdlib only)."""

import gzip
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))

import direct


def _frames(blob):
    out, i = [], 0
    while i < len(blob):
        flag = blob[i]
        size = int.from_bytes(blob[i + 1 : i + 5], "big")
        out.append((flag, blob[i + 5 : i + 5 + size]))
        i += 5 + size
    assert i == len(blob), "frame length mismatch"
    return out


def test_frames_are_two_gzip_connect_frames():
    frames = _frames(direct.build_frames("hello", "conv-1"))
    assert len(frames) == 2, frames
    assert all(flag == 1 for flag, _ in frames), "both frames must be gzip'd"
    run_request, exec_msg = (gzip.decompress(p) for _, p in frames)

    # AgentClientMessage.run_request(1) -> AgentRunRequest
    (field, wire, query), = direct.pb_fields(run_request)
    assert (field, wire) == (1, 2)

    # conversation_id (5) and requested_model (9) are present
    fields = {f: v for f, w, v in direct.pb_fields(query) if w == 2}
    assert fields[5] == b"conv-1"
    assert fields[9] == direct._pb_len(1, direct.MODEL)

    # action(2) -> user_message_action(1) -> user_message(1): three levels deep
    action = fields[2]
    (f1, w1, inner), = [x for x in direct.pb_fields(action) if x[0] == 1]
    (f2, w2, user), = [x for x in direct.pb_fields(inner) if x[0] == 1]
    assert (f1, f2) == (1, 1)
    text = [v for f, w, v in direct.pb_fields(user) if f == 1 and w == 2]
    assert text == [b"hello"], text

    # AgentClientMessage(2) -> exec client message with request_context_result(10)
    (f, w, exec_inner), = direct.pb_fields(exec_msg)
    assert (f, w) == (2, 2)
    assert 10 in [f for f, _, _ in direct.pb_fields(exec_inner)]


def test_iter_deltas_reassembles_split_frames():
    def server_msg(text):
        # AgentServerMessage.interaction_update(1) -> .text_delta(1) -> .text(1)
        delta = direct._pb_len(1, text.encode())
        return direct._pb_len(1, direct._pb_len(1, delta))

    def frame(payload, flag=0):
        return bytes([flag]) + len(payload).to_bytes(4, "big") + payload

    stream = (
        frame(server_msg("he"))
        + frame(gzip.compress(server_msg("llo")), flag=1)
        + frame(b'{"error":null}', flag=2)
        + frame(server_msg("never"), flag=0)  # must be ignored after end-stream
    )
    # split mid-header and mid-payload to exercise the buffer
    chunks = [stream[:2], stream[2:9], stream[9:17], stream[17:]]
    assert "".join(direct.iter_deltas(chunks)) == "hello"


def test_headers_carry_cli_identity():
    h = direct.headers("tok-123")
    assert h["user-agent"] == "connect-es/1.6.1"
    assert h["content-type"] == "application/connect+proto"
    assert h["authorization"] == "Bearer tok-123"
    assert h["connect-content-encoding"] == "gzip"
    assert len(h["x-blob-encryption-key"]) == 64


if __name__ == "__main__":
    test_frames_are_two_gzip_connect_frames()
    test_iter_deltas_reassembles_split_frames()
    test_headers_carry_cli_identity()
    print("test_direct: ok")
