import os
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

BRAIN, ADMIN, ENROLL = "b" * 32, "a" * 32, "e" * 32


def H(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def env():
    s = Settings("sqlite:///:memory:", BRAIN, ADMIN, ENROLL, online_window_s=30, lost_window_s=90, queue_ttl_s=900)
    app = create_app(s)
    with TestClient(app) as c:
        r = c.post("/api/v1/devices/register", json={"name": "lap", "info": {"os": "x"}}, headers=H(ENROLL))
        assert r.status_code == 201
        yield c, r.json()["device_id"], r.json()["device_token"], app


def mk(rid="r1", dev="", **kw):
    return {"request_id": rid, "device_id": dev, "task_type": "device_status", "arguments": {},
            "reason": "t", "timeout_seconds": 10, **kw}


def test_auth_separation(env):
    c, dev, dtok, _ = env
    assert c.post("/api/v1/tasks", json=mk(dev=dev)).status_code == 401
    assert c.post("/api/v1/tasks", json=mk(dev=dev), headers=H(dtok)).status_code == 403
    assert c.get("/api/v1/agent/tasks/next", headers=H(BRAIN)).status_code == 403
    assert c.get("/api/v1/admin/overview", headers=H(BRAIN)).status_code == 403
    assert c.post("/api/v1/devices/register", json={"name": "x"}, headers=H(BRAIN)).status_code == 403


def test_full_lifecycle_and_idempotency(env):
    c, dev, dtok, _ = env
    assert c.post("/api/v1/tasks", json=mk(dev=dev), headers=H(BRAIN)).status_code == 201
    assert c.post("/api/v1/tasks", json=mk(dev=dev), headers=H(BRAIN)).status_code == 200  # replay
    assert c.post("/api/v1/tasks", json=mk(dev=dev, arguments={"a": 1}), headers=H(BRAIN)).status_code == 409
    t = c.get("/api/v1/agent/tasks/next", headers=H(dtok)).json()["task"]
    assert t["request_id"] == "r1" and t["status"] == "claimed"
    assert c.get("/api/v1/agent/tasks/next", headers=H(dtok)).json()["task"] is None  # no double claim
    # result before approval is refused
    res = {"status": "completed", "exit_code": 0, "stdout": "ok"}
    assert c.post("/api/v1/tasks/r1/result", json=res, headers=H(dtok)).status_code == 409
    assert c.post("/api/v1/tasks/r1/decision", json={"decision": "approved", "risk": "auto"}, headers=H(dtok)).status_code == 200
    assert c.post("/api/v1/tasks/r1/result", json=res, headers=H(dtok)).status_code == 200
    assert c.post("/api/v1/tasks/r1/result", json=res, headers=H(dtok)).status_code == 200  # idempotent
    assert c.post("/api/v1/tasks/r1/result", json={**res, "status": "failed"}, headers=H(dtok)).status_code == 409
    got = c.get("/api/v1/tasks/r1", headers=H(BRAIN)).json()
    assert got["status"] == "completed" and got["stdout"] == "ok" and got["exit_code"] == 0


def test_reject_is_terminal(env):
    c, dev, dtok, _ = env
    c.post("/api/v1/tasks", json=mk(dev=dev), headers=H(BRAIN))
    c.get("/api/v1/agent/tasks/next", headers=H(dtok))
    assert c.post("/api/v1/tasks/r1/decision", json={"decision": "rejected"}, headers=H(dtok)).status_code == 200
    assert c.get("/api/v1/tasks/r1", headers=H(BRAIN)).json()["status"] == "rejected"
    assert c.post("/api/v1/tasks/r1/result", json={"status": "completed"}, headers=H(dtok)).status_code == 409


def test_serial_dispatch_per_device(env):
    c, dev, dtok, _ = env
    for i in (1, 2):
        c.post("/api/v1/tasks", json=mk(f"r{i}", dev), headers=H(BRAIN))
    assert c.get("/api/v1/agent/tasks/next", headers=H(dtok)).json()["task"]["request_id"] == "r1"
    assert c.get("/api/v1/agent/tasks/next", headers=H(dtok)).json()["task"] is None


def test_revoke_and_heartbeat_status(env):
    c, dev, dtok, _ = env
    assert c.get("/api/v1/devices/status", headers=H(BRAIN)).json()["devices"][0]["online"] is False
    c.post("/api/v1/devices/heartbeat", json={"agent_state": "running"}, headers=H(dtok))
    assert c.get("/api/v1/devices/status", headers=H(BRAIN)).json()["devices"][0]["online"] is True
    assert c.post(f"/api/v1/admin/devices/{dev}/revoke", headers=H(ADMIN)).status_code == 200
    assert c.post("/api/v1/devices/heartbeat", json={}, headers=H(dtok)).status_code == 403
    assert c.post("/api/v1/tasks", json=mk(dev=dev), headers=H(BRAIN)).status_code == 404


def test_lost_device_fails_inflight(env):
    from datetime import timedelta
    from app.db import Device, utcnow
    c, dev, dtok, app = env
    c.post("/api/v1/tasks", json=mk(dev=dev), headers=H(BRAIN))
    c.get("/api/v1/agent/tasks/next", headers=H(dtok))
    with app.state.Session() as db:
        db.get(Device, dev).last_seen = utcnow() - timedelta(seconds=500)
        db.commit()
    got = c.get("/api/v1/tasks/r1", headers=H(BRAIN)).json()
    assert got["status"] == "failed" and got["error"] == "device_disconnected"


def test_cancel_queued_and_signal(env):
    c, dev, dtok, _ = env
    c.post("/api/v1/tasks", json=mk(dev=dev), headers=H(BRAIN))
    assert c.post("/api/v1/tasks/r1/cancel", headers=H(BRAIN)).json()["status"] == "cancelled"
    c.post("/api/v1/tasks", json=mk("r2", dev), headers=H(BRAIN))
    c.get("/api/v1/agent/tasks/next", headers=H(dtok))
    c.post("/api/v1/tasks/r2/cancel", headers=H(BRAIN))
    assert c.post("/api/v1/devices/heartbeat", json={}, headers=H(dtok)).json()["cancel_requests"] == ["r2"]


def test_validation_and_dashboard(env):
    c, dev, dtok, _ = env
    assert c.post("/api/v1/tasks", json=mk(dev=dev, task_type="rm_rf"), headers=H(BRAIN)).status_code == 422
    assert c.post("/api/v1/tasks", json=mk("bad id!", dev), headers=H(BRAIN)).status_code == 422
    assert c.get("/dashboard").status_code == 200
    o = c.get("/api/v1/admin/overview", headers=H(ADMIN)).json()
    assert o["devices"][0]["device_id"] == dev


def test_two_way_messages(env):
    c, dev, dtok, _ = env
    assert c.post("/api/v1/agent/messages", json={"text": "hello brain"}, headers=H(dtok)).status_code == 201
    r = c.get(f"/api/v1/messages?device_id={dev}", headers=H(BRAIN)).json()
    assert [m["text"] for m in r["messages"]] == ["hello brain"] and r["last_id"] >= 1
    assert c.get(f"/api/v1/messages?device_id={dev}&after_id={r['last_id']}", headers=H(BRAIN)).json()["messages"] == []
    assert c.post("/api/v1/messages", json={"device_id": dev, "text": "hi om"}, headers=H(BRAIN)).status_code == 201
    got = c.get("/api/v1/agent/messages", headers=H(dtok)).json()
    assert [m["text"] for m in got["messages"]] == ["hi om"]          # device only sees brain messages
    assert c.get("/api/v1/agent/messages", headers=H(BRAIN)).status_code == 403
    assert c.post("/api/v1/messages", json={"device_id": dev, "text": "x"}, headers=H(dtok)).status_code == 403
    allm = c.get(f"/api/v1/messages?device_id={dev}&direction=all", headers=H(BRAIN)).json()["messages"]
    assert [m["direction"] for m in allm] == ["user", "brain"]
