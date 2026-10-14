import httpx


class RelayError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"{status}: {msg}")
        self.status = status

    @property
    def permanent(self) -> bool:
        return 400 <= self.status < 500 and self.status not in (408, 425, 429)


class RelayClient:
    def __init__(self, base_url: str, device_token: str, timeout: float = 15.0):
        self._c = httpx.Client(base_url=base_url, timeout=timeout,
                               headers={"Authorization": f"Bearer {device_token}",
                                        "User-Agent": "laptop-agent/1.0"})

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self._c.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("detail", r.text)
            except Exception:
                msg = r.text[:200]
            raise RelayError(r.status_code, str(msg))
        return r.json()

    def heartbeat(self, state: str, active: str | None) -> dict:
        return self._req("POST", "/api/v1/devices/heartbeat",
                         json={"agent_state": state, "active_request_id": active})

    def next_task(self) -> dict | None:
        return self._req("GET", "/api/v1/agent/tasks/next").get("task")

    def decision(self, rid: str, decision: str, risk: str | None, note: str | None) -> dict:
        return self._req("POST", f"/api/v1/tasks/{rid}/decision",
                         json={"decision": decision, "risk": risk, "note": note})

    def result(self, rid: str, payload: dict) -> dict:
        return self._req("POST", f"/api/v1/tasks/{rid}/result", json=payload)

    def send_message(self, text: str, channel: str = "app", to_agent: str | None = None,
                     context: dict | None = None) -> dict:
        body = {"text": text, "channel": channel}
        if to_agent:
            body["to_agent"] = to_agent
        if context is not None:
            body["context"] = context
        return self._req("POST", "/api/v1/agent/messages", json=body)

    def sync(self, after_id: int) -> dict:
        return self._req("GET", "/api/v1/agent/sync", params={"after_id": after_id, "limit": 50})

    def route_message(self, msg_id: int, to_agent: str, context: dict | None) -> dict:
        return self._req("POST", f"/api/v1/agent/messages/{msg_id}/route", json={"to_agent": to_agent, "context": context})

    def cancel_message(self, msg_id: int) -> dict:
        return self._req("POST", f"/api/v1/agent/messages/{msg_id}/cancel")

    def rename_agent(self, agent_id: str, name: str) -> dict:
        return self._req("POST", f"/api/v1/agent/agents/{agent_id}/rename", json={"name": name})

    def brain_messages(self, after_id: int) -> dict:
        return self._req("GET", "/api/v1/agent/messages", params={"after_id": after_id, "limit": 20})

    def close(self) -> None:
        self._c.close()


def register(relay_url: str, enroll_token: str, name: str, info: dict) -> dict:
    r = httpx.post(relay_url.rstrip("/") + "/api/v1/devices/register", timeout=20,
                   headers={"Authorization": f"Bearer {enroll_token}"},
                   json={"name": name, "info": info})
    if r.status_code != 201:
        raise RelayError(r.status_code, r.text[:200])
    return r.json()
