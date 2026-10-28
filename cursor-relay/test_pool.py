#!/usr/bin/env python3
"""Self-check for the card-pool renewal decision tree. Run: python3 test_pool.py"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

import pool


def _isolate(tmp: Path) -> None:
    pool.CARDS_FILE = tmp / "cards.json"
    pool.HISTORY_FILE = tmp / "renewal_history.json"
    pool.ACTIVE_TOKEN = tmp / ".cursor-tk" / "active_token"
    pool.STATE_FILE = tmp / ".cursor-tk" / "kya_switch_state.json"
    pool.DEVICE_ID_FILE = tmp / "device_id.txt"


def _stub_post(script):
    """script: path -> list of responses, consumed in order."""
    calls = []

    def fake(path, code):
        calls.append((path, code))
        seq = script[path]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return fake, calls


def test_plain_success():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool._post, calls = _stub_post({pool.R1_PATH: [{"success": True, "token": "T" * 40}]})
        ok, token, msg = pool.renew_card({"cardCode": "C1"})
        assert ok and token == "T" * 40 and msg == "", (ok, token, msg)
        assert len(calls) == 1, calls


def test_limit_reached_resets_then_renews():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool._post, calls = _stub_post({
            pool.R1_PATH: [{"limitReached": True}, {"success": True, "token": "R" * 40}],
            pool.RS1_PATH: [{"success": True}],
        })
        ok, token, msg = pool.renew_card({"cardCode": "C1"})
        assert ok and token == "R" * 40 and msg == "reset+renew", (ok, token, msg)
        assert [c[0] for c in calls] == [pool.R1_PATH, pool.RS1_PATH, pool.R1_PATH], calls


def test_reset_failure_reports_message():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool._post = lambda path, code: (
            {"limitReached": True} if path == pool.R1_PATH else {"success": False, "message": "卡密已过期"}
        )
        ok, token, msg = pool.renew_card({"cardCode": "C1"})
        assert not ok and token == "" and msg == "卡密已过期", (ok, token, msg)


def test_pool_rotates_past_dead_key():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({
            "active": 0,
            "cards": [{"email": "dead@x", "cardCode": "DEAD"}, {"email": "live@x", "cardCode": "LIVE"}],
        })

        def fake(path, code):
            if code == "LIVE":
                return {"success": True, "token": "L" * 40}
            return {"success": False, "message": "无效卡密"}

        pool._post = fake
        ok, token, detail = pool.renew_next_card()
        assert ok and token == "L" * 40 and detail == "live@x", (ok, token, detail)
        assert pool.load_pool()["active"] == 1
        assert pool.read_active_token() == "L" * 40


def test_pool_reports_only_after_all_keys_fail():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({
            "active": 0,
            "cards": [{"email": "a@x", "cardCode": "A"}, {"email": "b@x", "cardCode": "B"}],
        })
        pool._post = lambda path, code: {"success": False, "message": "无效卡密"}
        ok, token, detail = pool.renew_next_card()
        assert not ok and token == ""
        assert "a@x" in detail and "b@x" in detail, detail
        assert pool.read_active_token() == ""


def test_trigger_and_wait_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool._post = lambda path, code: {"success": True, "token": "Z" * 40}
        pool._running = False
        since = time.time() * 1000
        pool.trigger_quota_switch("test")
        assert pool.wait_for_quota_renewal(since, timeout_s=10), pool.read_state()
        assert pool.read_active_token() == "Z" * 40
        assert pool.read_state()["status"] == "success"
        assert pool.snapshot()["cards"][0]["renewCount"] == 1


def test_wait_times_out_when_renewal_fails():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool._post = lambda path, code: {"success": False, "message": "无效卡密"}
        pool._running = False
        since = time.time() * 1000
        pool.trigger_quota_switch("test")
        assert pool.wait_for_quota_renewal(since, timeout_s=10) is False
        assert pool.read_state()["status"] == "error"


def test_reset_device_id_backs_up_and_rotates():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.DEVICE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        pool.DEVICE_ID_FILE.write_text("OLD-ID", encoding="utf-8")

        seen = []
        pool._post = lambda path, code: (
            seen.append(pool.device_id()), {"success": True, "token": "N" * 40}
        )[1]
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool.renew_next_card()

        new, old = pool.reset_device_id()
        assert old == "OLD-ID" and new != old, (old, new)
        assert pool.device_id() == new
        backups = list(pool.DEVICE_ID_FILE.parent.glob("device_id.txt.bak.*"))
        assert backups and backups[0].read_text(encoding="utf-8") == "OLD-ID", backups

        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool.renew_next_card()
        assert seen == ["OLD-ID", new], seen


def test_history_records_each_renewal():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool._post = lambda path, code: {"success": True, "token": "Z" * 40}
        pool._running = False
        since = time.time() * 1000
        pool.trigger_quota_switch("test")
        assert pool.wait_for_quota_renewal(since, timeout_s=10)

        pool.save_pool({"active": 0, "cards": [{"email": "b@x", "cardCode": "B"}]})
        pool._post = lambda path, code: {"success": False, "message": "无效卡密"}
        since = time.time() * 1000
        pool.trigger_quota_switch("test")
        assert pool.wait_for_quota_renewal(since, timeout_s=10) is False

        history = pool.load_history()
        assert len(history) == 2, history
        assert history[0]["ok"] is False and "无效卡密" in history[0]["message"], history[0]
        assert history[1]["ok"] is True and history[1]["card"] == "a@x", history[1]
        assert len(pool.snapshot()["history"]) == 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all pool checks passed")
