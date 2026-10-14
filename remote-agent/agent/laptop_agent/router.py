"""The middle guy. Decides which agent handles which chat, keeps each chat's context on THIS laptop, and hands a
chat to another agent (with the same context) when an agent is offline, silent or removed. Deterministic code,
not an LLM: cheap, predictable and always on."""
import re
import threading
import time
from collections import Counter

from .client import RelayError

MENTION = re.compile(r"^\s*@([A-Za-z0-9_.-]+)\b[\s,:]*")
NAME_PREFIX = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*[,:]\s+")
MAX_HANDOFFS = 3


class Router:
    def __init__(self, engine, store):
        self.engine, self.store = engine, store
        self.agents: dict[str, dict] = {}
        self.after_id = int(store.get_meta("after_id", 0) or 0)
        self.pending: dict[int, dict] = {}     # relay message id -> dispatch record (not yet picked up by the agent)
        self.waiting: list[dict] = []          # messages that could not be dispatched (no agent online)
        self.inflight: dict[str, dict] = {}    # channel -> {agent, since, text, ...} (user message awaiting an answer)
        self.seen_unrouted: dict[int, int] = {}
        self.lock = threading.RLock()
        self.inline = False                    # tests: run dispatches synchronously

    # ------------------------------------------------------------- helpers
    @property
    def client(self):
        return self.engine._client

    def name_of(self, agent_id: str | None) -> str:
        a = self.agents.get(agent_id or "")
        return a["name"] if a else (agent_id or "an agent")

    def online(self, agent_id: str | None) -> bool:
        return bool(agent_id) and bool(self.agents.get(agent_id, {}).get("online"))

    def find(self, ref: str | None) -> str | None:
        """'nova' / 'Nova' / an agent id -> agent id (only known agents)."""
        r = (ref or "").strip().lower()
        for a in self.agents.values():
            if r in (a["agent_id"].lower(), a["name"].lower()):
                return a["agent_id"]
        return None

    def system_note(self, channel: str, text: str):
        self.store.add_message(channel, "sys", text)
        self.engine._add_chat("sys", text, channel)

    def pick(self, channel: str, exclude=()) -> str | None:
        chat = self.store.chat(channel) or {}
        ok = {a["agent_id"] for a in self.agents.values() if a["online"] and a["agent_id"] not in exclude}
        for choice in (chat.get("pinned"), chat.get("assigned")):      # pinned wins, then sticky
            if choice in ok:
                return choice
        if not ok:
            return None
        load = Counter(self.store.load_counts())
        load.update(p["to"] for p in self.pending.values())
        return min(ok, key=lambda i: (load.get(i, 0), self.name_of(i).lower()))

    def _spawn(self, fn, *a):
        if self.inline:
            fn(*a)
        else:
            threading.Thread(target=fn, args=a, daemon=True).start()

    # ------------------------------------------------- user -> agents (app, local API)
    def route_user_message(self, text: str, channel: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        with self.lock:
            self.store.ensure_chat(channel)
            local_id = self.store.add_message(channel, "you", text)
            self.engine._add_chat("you", text, channel)
            if text.lower() == "stop":
                self.inflight.pop(channel, None)
                self._spawn(self._broadcast_stop, channel, local_id)
                return True
            called = self._called(text)
            agent = None
            if called:
                if self.online(called):
                    agent = called
                    self.store.set_assigned(channel, called)
                else:
                    self.system_note(channel, f"{self.name_of(called)} is offline, so I am sending this to another agent.")
            agent = agent or self.pick(channel)
            if agent is None:
                self.waiting.append({"channel": channel, "text": text, "local_id": local_id, "handoffs": 0, "tried": set()})
                self.store.update_message(local_id, status="queued")
                self.system_note(channel, "No agent is online yet. I will deliver this as soon as one connects.")
                return True
            self._spawn(self._dispatch, channel, text, local_id, agent, None, 0, set())
            return True

    def _called(self, text: str) -> str | None:
        m = MENTION.match(text) or NAME_PREFIX.match(text)
        return self.find(m.group(1)) if m else None

    def _dispatch(self, channel, text, local_id, agent, handoff, handoffs, tried):
        with self.lock:
            packet = self.store.packet(channel, exclude_id=local_id)
            packet["handoff"] = handoff
            packet["you_are"] = {"agent_id": agent, "name": self.name_of(agent)}
            packet["other_agents"] = [{"agent_id": a["agent_id"], "name": a["name"], "online": a["online"]}
                                      for a in self.agents.values() if a["agent_id"] != agent]
        try:
            r = self.client.send_message(text, channel, to_agent=agent, context=packet)
        except RelayError as e:
            with self.lock:
                self.agents.get(agent, {})["online"] = False if e.status == 404 else self.agents.get(agent, {}).get("online")
                self.waiting.append({"channel": channel, "text": text, "local_id": local_id, "handoffs": handoffs, "tried": tried})
                self.store.update_message(local_id, status="queued")
            self.engine.notify("warning", f"Could not deliver to {self.name_of(agent)}: {e}")
            return
        except Exception as e:  # network
            with self.lock:
                self.waiting.append({"channel": channel, "text": text, "local_id": local_id, "handoffs": handoffs, "tried": tried})
            self.engine.notify("warning", f"Relay unreachable; message queued ({type(e).__name__})")
            return
        with self.lock:
            self.pending[r["id"]] = {"channel": channel, "text": text, "to": agent, "ts": time.time(),
                                     "local_id": local_id, "handoffs": handoffs, "tried": set(tried) | {agent}}
            self.store.update_message(local_id, status="sent", relay_id=r["id"])
            self.store.set_assigned(channel, agent)
            self.inflight[channel] = {"agent": agent, "since": time.time(), "text": text, "local_id": local_id,
                                      "warned": False, "noted": False, "handoffs": handoffs}
        self.engine._emit("routed", {"channel": channel, "agent": agent})

    def _broadcast_stop(self, channel, local_id):
        sent = 0
        for a in list(self.agents.values()):
            try:
                self.client.send_message("stop", channel, to_agent=a["agent_id"], context={"stop": True, "channel": channel})
                sent += 1
            except Exception:
                pass
        self.store.update_message(local_id, status="sent" if sent else "queued")
        if not sent:
            self.system_note(channel, "No agents are known yet, so there is nobody to stop.")

    # ------------------------------------------------------------- sync loop
    def sync_once(self):
        r = self.client.sync(self.after_id)
        with self.lock:
            self.agents = {a["agent_id"]: a for a in r["agents"]}
            for m in r["messages"]:
                self._on_brain_message(m)
            if r["last_id"] != self.after_id:
                self.after_id = r["last_id"]
                self.store.set_meta("after_id", self.after_id)
            for m in r["unrouted"]:
                self._route_inbound(m)
            still = {p["id"] for p in r["pending"]}
            now = time.time()
            for rid, p in list(self.pending.items()):
                if rid not in still:                         # picked up (or retracted by us)
                    self.pending.pop(rid)
                    self.store.update_message(p["local_id"], status="delivered")
                elif not self.online(p["to"]):
                    self._failover(rid, p, "is offline")
                elif now - p["ts"] > self.engine.cfg.ack_timeout_s:
                    self._failover(rid, p, f"did not pick it up within {int(self.engine.cfg.ack_timeout_s)}s")
            self._check_inflight(now)
            self._flush_waiting()
            for ch in [c for c, f in self.inflight.items() if now - f["since"] > 1800]:
                self.inflight.pop(ch)

    def _on_brain_message(self, m: dict):
        ch, agent = m["channel"], m.get("from_agent")
        self.store.ensure_chat(ch)
        if m.get("kind") == "notes":
            self.store.set_notes(ch, m["text"], agent)
            self.engine._emit("context", ch)
            return
        if self.store.has_relay_message(m["id"], "brain"):
            return
        self.store.add_message(ch, "brain", m["text"], agent=agent, relay_id=m["id"])
        self.engine._add_chat("brain", m["text"], ch, agent)
        self.inflight.pop(ch, None)
        if agent:
            self.store.set_assigned(ch, agent)

    def _route_inbound(self, m: dict):
        """A message that arrived through the relay chat API: the laptop router picks its agent."""
        ch, text, mid = m["channel"], m["text"], m["id"]
        if mid not in self.seen_unrouted:
            self.store.ensure_chat(ch)
            self.seen_unrouted[mid] = self.store.add_message(ch, "you", text, relay_id=mid, status="routing")
            self.engine._add_chat("you", text, ch)
        local_id = self.seen_unrouted[mid]
        called = self._called(text)
        agent = called if self.online(called) else self.pick(ch)
        if agent is None:
            return                                      # stays unrouted; retried on the next sync
        packet = self.store.packet(ch, exclude_id=local_id)
        packet["you_are"] = {"agent_id": agent, "name": self.name_of(agent)}
        packet["handoff"] = None
        try:
            self.client.route_message(mid, agent, packet)
        except RelayError:
            self.seen_unrouted.pop(mid, None)
            return
        self.pending[mid] = {"channel": ch, "text": text, "to": agent, "ts": time.time(), "local_id": local_id,
                             "handoffs": 0, "tried": {agent}}
        self.store.set_assigned(ch, agent)
        self.store.update_message(local_id, status="sent")
        self.inflight[ch] = {"agent": agent, "since": time.time(), "text": text, "local_id": local_id,
                             "warned": False, "noted": False, "handoffs": 0}

    # ------------------------------------------------------------- failover
    def _failover(self, rid: int, p: dict, reason: str):
        try:
            res = self.client.cancel_message(rid)
        except Exception:
            return                                        # cannot retract right now: try again next tick
        self.pending.pop(rid, None)
        if res.get("delivered"):                          # it was picked up in the meantime: nothing to do
            self.store.update_message(p["local_id"], status="delivered")
            return
        self._handover(p["channel"], p["text"], p["local_id"], p["to"], reason, p["handoffs"] + 1, p["tried"], False)

    def _handover(self, ch, text, local_id, from_agent, reason, handoffs, tried, continuation: bool):
        if handoffs > MAX_HANDOFFS:
            self.system_note(ch, f"I tried {MAX_HANDOFFS} agents and none could take this. Please try again later.")
            return
        new = self.pick(ch, exclude=set(tried) | {from_agent})
        frm = self.name_of(from_agent)
        if new is None:
            self.waiting.append({"channel": ch, "text": text, "local_id": local_id, "handoffs": handoffs,
                                 "tried": set(tried) | {from_agent}, "from": from_agent, "continuation": continuation})
            self.store.set_assigned(ch, None)
            self.system_note(ch, f"{frm} {reason}. No other agent is online; I will hand this over when one connects.")
            return
        self.store.add_handoff(ch, frm, self.name_of(new), reason)
        self.store.add_event(ch, "handoff", f"{frm} {reason}; handed to {self.name_of(new)}")
        self.system_note(ch, f"↪ {frm} {reason}. Handed over to {self.name_of(new)} with the same chat context.")
        body = text
        if continuation:
            body = (f"[handoff] {frm} went offline while working on this chat. The user's last message was: {text}\n"
                    "Read the context packet (notes + recent task events) before repeating any work, then continue.")
        self._spawn(self._dispatch, ch, body, local_id, new,
                    {"from": frm, "to": self.name_of(new), "reason": reason, "continuation": continuation},
                    handoffs, set(tried) | {from_agent})

    def _check_inflight(self, now: float):
        for ch, f in list(self.inflight.items()):
            if any(p["channel"] == ch for p in self.pending.values()):
                continue                                   # not even picked up yet: the pending logic handles it
            if not self.online(f["agent"]):
                if self.pick(ch, exclude={f["agent"]}) is None:
                    if not f["noted"]:
                        f["noted"] = True
                        self.system_note(ch, f"{self.name_of(f['agent'])} went offline and no other agent is available yet.")
                    continue
                self.inflight.pop(ch)
                self._handover(ch, f["text"], f["local_id"], f["agent"], "went offline", f["handoffs"] + 1,
                               {f["agent"]}, True)
            elif now - f["since"] > self.engine.cfg.stall_warn_s and not f["warned"]:
                f["warned"] = True
                self.system_note(ch, f"{self.name_of(f['agent'])} has been quiet for {int(self.engine.cfg.stall_warn_s // 60)}+ "
                                     "minutes on this chat. You can reassign it from the chat header.")

    def _flush_waiting(self):
        if not self.waiting:
            return
        items, self.waiting = self.waiting, []
        for w in items:
            agent = self.pick(w["channel"], exclude=set())
            if agent is None:
                self.waiting.append(w)
                continue
            body = w["text"]
            if w.get("continuation"):
                body = (f"[handoff] {self.name_of(w.get('from'))} went offline while working on this chat. The user's last "
                        f"message was: {w['text']}\nRead the context packet before repeating any work, then continue.")
            self._spawn(self._dispatch, w["channel"], body, w["local_id"], agent,
                        {"from": self.name_of(w.get("from")), "to": self.name_of(agent), "reason": "delivered late",
                         "continuation": bool(w.get("continuation"))} if w.get("from") else None,
                        w["handoffs"], w["tried"])

    # ------------------------------------------------------------- activity + UI actions
    def note_activity(self, channel: str | None, agent: str | None):
        f = self.inflight.get(channel or "")
        if f and (agent is None or agent == f["agent"]):
            f["since"], f["warned"] = time.time(), False

    def pin(self, channel: str, agent: str | None):
        with self.lock:
            self.store.ensure_chat(channel)
            self.store.set_pinned(channel, agent)
            if agent:
                self.store.set_assigned(channel, agent)

    def reassign(self, channel: str):
        """User pressed 'reassign': hand the chat's open work to another agent right now."""
        with self.lock:
            chat = self.store.chat(channel) or {}
            cur = chat.get("assigned")
            self.store.set_pinned(channel, None)
            for rid, p in list(self.pending.items()):
                if p["channel"] == channel:
                    self._failover(rid, p, "was reassigned by you")
                    return
            f = self.inflight.pop(channel, None)
            self.store.set_assigned(channel, None)
            if f:
                self._handover(channel, f["text"], f["local_id"], f["agent"], "was reassigned by you",
                               f["handoffs"] + 1, {f["agent"]}, True)
            elif cur:
                self.system_note(channel, f"Chat unassigned from {self.name_of(cur)}; the next message picks the best agent.")
