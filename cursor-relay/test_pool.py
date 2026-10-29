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
    pool.DAILY_FILE = tmp / "daily_switch.json"
    pool.AUTO_RENEW_FILE = tmp / "auto_renew.json"
    # 多数用例走 auto 触发路径，默认打开开关；开关语义本身由专门用例覆盖
    pool.set_auto_renew(True)
    pool.RETRY_SLEEP_S = 0
    pool.COOLDOWN_S = 0
    pool._last_switch_at = 0.0
    pool._running = False


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


def test_success_flag_required_even_with_long_token():
    """KYA: success && token.length>20 才算成功；只有 token 不算。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        calls = []

        def fake(path, code):
            calls.append(path)
            return {"success": False, "token": "T" * 40, "message": "no"}

        pool._post = fake
        ok, token, msg = pool.renew_card({"cardCode": "C1"})
        assert not ok and token == "" and msg == "no", (ok, token, msg)
        assert len(calls) == 2, calls


def test_first_failure_triggers_single_silent_retry():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool._post, calls = _stub_post({
            pool.R1_PATH: [{"success": False, "message": "busy"}, {"success": True, "token": "T" * 40}],
        })
        ok, token, msg = pool.renew_card({"cardCode": "C1"})
        assert ok and token == "T" * 40 and msg == "", (ok, token, msg)
        assert [c[0] for c in calls] == [pool.R1_PATH, pool.R1_PATH], calls


def test_limit_reached_resets_then_renews():
    """KYA: rs1 返回的 token 直接作为新凭据，不再补一次 r1。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool._post, calls = _stub_post({
            pool.R1_PATH: [{"limitReached": True}],
            pool.RS1_PATH: [{"success": True, "token": "R" * 40}],
        })
        ok, token, msg = pool.renew_card({"cardCode": "C1"})
        assert ok and token == "R" * 40 and msg == "reset", (ok, token, msg)
        assert [c[0] for c in calls] == [pool.R1_PATH, pool.RS1_PATH], calls
        assert pool.load_daily()["resets"] == 1


def test_reset_failure_reports_message():
    """KYA: 重置未通过 -> 直接轮换备用卡密，不重试本卡。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        calls = []

        def fake(path, code):
            calls.append(path)
            return (
                {"limitReached": True}
                if path == pool.R1_PATH
                else {"success": False, "message": "卡密已过期"}
            )

        pool._post = fake
        ok, token, msg = pool.renew_card({"cardCode": "C1"})
        assert not ok and token == "" and msg == "卡密已过期", (ok, token, msg)
        assert calls == [pool.R1_PATH, pool.RS1_PATH], calls


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
        ok, token, action, index, detail = pool.renew_next_card()
        assert ok and token == "L" * 40 and detail == "live@x", (ok, token, detail)
        assert action == "renew" and index == 1, (action, index)
        assert pool.load_pool()["active"] == 1
        assert pool.read_active_token() == "L" * 40
        assert pool.load_daily()["switches"] == 0, "轮换备用卡后今日换号计数应清零"


def test_pool_does_not_wrap_to_earlier_cards():
    """KYA rotateToNextCard: nextIdx >= pool.count 即失败，不绕回池首。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({
            "active": 1,
            "cards": [{"email": "live@x", "cardCode": "LIVE"}, {"email": "dead@x", "cardCode": "DEAD"}],
        })
        seen = []

        def fake(path, code):
            seen.append(code)
            if code == "LIVE":
                return {"success": True, "token": "L" * 40}
            return {"success": False, "message": "无效卡密"}

        pool._post = fake
        ok, _token, action, _idx, _detail = pool.renew_next_card()
        assert not ok and action == "failed", (ok, action)
        assert seen and set(seen) == {"DEAD"}, seen
        assert pool.load_pool()["active"] == 1


def test_pool_reports_only_after_all_keys_fail():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({
            "active": 0,
            "cards": [{"email": "a@x", "cardCode": "A"}, {"email": "b@x", "cardCode": "B"}],
        })
        pool._post = lambda path, code: {"success": False, "message": "无效卡密"}
        ok, token, action, _idx, detail = pool.renew_next_card()
        assert not ok and token == "" and action == "failed", (ok, token, action)
        assert "a@x" in detail and "b@x" in detail, detail
        assert pool.read_active_token() == ""


def test_trigger_and_wait_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool._post = lambda path, code: {"success": True, "token": "Z" * 40}
        since = time.time() * 1000
        pool.trigger_quota_switch("test")
        assert pool.wait_for_quota_renewal(since, timeout_s=10), pool.read_state()
        assert pool.read_active_token() == "Z" * 40
        state = pool.read_state()
        assert state["status"] == "success" and state["phase"] == "completed", state
        assert pool.snapshot()["cards"][0]["renewCount"] == 1


def test_wait_times_out_when_renewal_fails():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool._post = lambda path, code: {"success": False, "message": "无效卡密"}
        since = time.time() * 1000
        pool.trigger_quota_switch("test")
        assert pool.wait_for_quota_renewal(since, timeout_s=10) is False
        state = pool.read_state()
        assert state["status"] == "error" and state["phase"] == "pool-exhausted", state


def test_manual_switch_skipped_during_cooldown():
    """KYA: 非 Quota 类原因在冷却期内直接跳过。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.COOLDOWN_S = 30
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        calls = []
        pool._post = lambda path, code: (calls.append(path), {"success": True, "token": "Z" * 40})[1]
        pool._last_switch_at = time.time()
        pool.trigger_quota_switch("cursor-admin")
        assert calls == [], calls
        assert pool._running is False
        state = pool.read_state()
        assert state["status"] == "skipped" and state["phase"] == "cooldown", state


def test_auto_switch_queues_after_cooldown():
    """KYA: Quota 类原因在冷却期内排队，冷却结束后自动执行。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.COOLDOWN_S = 0.3
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool._post = lambda path, code: {"success": True, "token": "Z" * 40}
        pool._last_switch_at = time.time()
        since = time.time() * 1000
        pool.trigger_quota_switch("cursor-relay")
        assert pool._running is False, "冷却中应排队而非立即执行"
        assert pool.wait_for_quota_renewal(since, timeout_s=10), pool.read_state()
        assert pool.read_active_token() == "Z" * 40


def test_auto_renew_defaults_off_and_only_gates_auto():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.AUTO_RENEW_FILE.unlink(missing_ok=True)
        assert pool.auto_renew_enabled() is False

        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        calls = []
        pool._post = lambda path, code: (calls.append(path), {"success": True, "token": "Z" * 40})[1]

        pool.trigger_quota_switch("cursor-relay")
        assert calls == [] and pool._running is False, calls
        state = pool.read_state()
        assert state["status"] == "skipped" and state["phase"] == "auto_disabled", state
        assert pool.snapshot()["autoRenew"] is False

        # 手动续杯不受开关影响
        since = time.time() * 1000
        pool.trigger_quota_switch("cursor-admin")
        assert pool.wait_for_quota_renewal(since, timeout_s=10), pool.read_state()

        # 打开开关后自动路径恢复
        pool.set_auto_renew(True)
        assert pool.snapshot()["autoRenew"] is True
        since = time.time() * 1000
        pool.trigger_quota_switch("cursor-relay")
        assert pool.wait_for_quota_renewal(since, timeout_s=10), pool.read_state()


def test_daily_counters_and_quota_math():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool._post = lambda path, code: {"success": True, "token": "Z" * 40}
        since = time.time() * 1000
        pool.trigger_quota_switch("cursor-admin")
        assert pool.wait_for_quota_renewal(since, timeout_s=10)

        daily = pool.load_daily()
        assert daily["switches"] == 1 and daily["resets"] == 0, daily
        assert pool.effective_daily_total(daily) == 60, daily

        pool._post = lambda path, code: (
            {"limitReached": True} if path == pool.R1_PATH else {"success": True, "token": "R" * 40}
        )
        assert pool.renew_card({"cardCode": "A"})[0]
        snap = pool.snapshot()["daily"]
        assert snap["resets"] == 1 and snap["total"] == 120, snap
        assert snap["remaining"] == 119, snap


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


def test_history_distinguishes_manual_and_auto():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        pool._post = lambda path, code: {"success": True, "token": "Z" * 40}
        for session in ("cursor-admin", "cursor-relay"):
            since = time.time() * 1000
            pool.trigger_quota_switch(session)
            assert pool.wait_for_quota_renewal(since, timeout_s=10), pool.read_state()

        reasons = [h["reason"] for h in pool.load_history()]
        assert reasons == ["auto", "manual"], reasons


def test_snapshot_history_caps_at_five():
    """控制台快照只回最近 5 条；完整列表走 /admin/history，避免轮询渲染变慢。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        for i in range(8):
            pool.add_history({"at": i, "ok": True, "card": f"c{i}", "message": "", "reason": "manual"})

        history = pool.snapshot()["history"]
        assert pool.SNAPSHOT_HISTORY == 5, pool.SNAPSHOT_HISTORY
        assert [h["at"] for h in history] == [7, 6, 5, 4, 3], history
        assert len(pool.load_history()) == 8, "文件仍保留全部记录"


def test_expired_card_skipped_without_cloud_call():
    """endTime 已过的卡本地直接跳过，一次云端请求都不打。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({
            "active": 0,
            "cards": [
                {"email": "old@x", "cardCode": "OLD", "endTime": "2020-01-01"},
                {"email": "live@x", "cardCode": "LIVE"},
            ],
        })
        seen = []

        def fake(path, code):
            seen.append(code)
            return {"success": True, "token": "L" * 40}

        pool._post = fake
        ok, token, action, idx, detail = pool.renew_next_card()
        assert ok and token == "L" * 40 and detail == "live@x", (ok, token, detail)
        assert seen == ["LIVE"], seen
        assert action == "renew" and idx == 1
        assert pool.snapshot()["cards"][0]["expired"] is True


def test_reset_quota_capped_per_card():
    """每卡每日 1 次、累计 10 次：超了本地拒绝，绝不再打 /rs1（真扣时长）。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({"active": 0, "cards": [{"email": "a@x", "cardCode": "A"}]})
        calls = []

        def fake(path, code):
            calls.append(path)
            if path == pool.R1_PATH:
                return {"limitReached": True}
            return {"success": True, "token": "R" * 40}

        pool._post = fake
        ok, _token, action, _idx, _detail = pool.renew_next_card()
        assert ok and action == "reset", (ok, action)
        card = pool.load_pool()["cards"][0]
        assert card["resetCount"] == 1 and card["resetDayCount"] == 1, card

        # 当天第二次：只在 /r1 报满后本地拦下
        calls.clear()
        ok, _token, action, _idx, detail = pool.renew_next_card()
        assert not ok and action == "failed" and "reset-capped" in detail, detail
        assert pool.RS1_PATH not in calls, calls
        assert pool.snapshot()["cards"][0]["resetDayCount"] == 1

        # 累计上限独立于当日计数
        p = pool.load_pool()
        p["cards"][0] = {"email": "a@x", "cardCode": "A",
                         "resetCount": pool.RESET_LIFETIME_MAX, "resetDate": "", }
        pool.save_pool(p)
        assert pool.reset_capped(pool.load_pool()["cards"][0]) == "累计重置已达上限 10 次"
        calls.clear()
        assert pool.renew_next_card()[2] == "failed"
        assert pool.RS1_PATH not in calls, calls


def test_history_records_action_and_index():
    """history 能区分 renew / reset / failed，并带上卡池下标。"""
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        pool.save_pool({
            "active": 0,
            "cards": [{"email": "a@x", "cardCode": "A"}, {"email": "b@x", "cardCode": "B"}],
        })
        pool._post = lambda path, code: {"success": True, "token": "Z" * 40}
        since = time.time() * 1000
        pool.trigger_quota_switch("cursor-admin")
        assert pool.wait_for_quota_renewal(since, timeout_s=10)

        pool._post = lambda path, code: (
            {"limitReached": True} if path == pool.R1_PATH else {"success": True, "token": "R" * 40}
        )
        since = time.time() * 1000
        pool.trigger_quota_switch("cursor-admin")
        assert pool.wait_for_quota_renewal(since, timeout_s=10)

        p = pool.load_pool()
        p["cards"] = [{"email": "dead@x", "cardCode": "D"}]
        p["active"] = 0
        pool.save_pool(p)
        pool._post = lambda path, code: {"success": False, "message": "无效卡密"}
        since = time.time() * 1000
        pool.trigger_quota_switch("cursor-admin")
        assert pool.wait_for_quota_renewal(since, timeout_s=10) is False

        history = pool.load_history()
        assert [h["action"] for h in history] == ["failed", "reset", "renew"], history
        assert history[2]["card"] == "a@x" and history[2]["index"] == 0, history[2]
        assert history[0]["card"] == "" and "无效卡密" in history[0]["message"], history[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all pool checks passed")
