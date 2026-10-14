"""The router (middle guy) against a fake relay, so every failure mode can be forced deterministically."""
import time
import pytest
from laptop_agent.router import Router
from laptop_agent.store import ChatStore


class FakeRelay:
    def __init__(self):
        self.msgs, self.agents, self.brain, self.unrouted, self.n = {}, {}, [], [], 0

    def add_agent(self, aid, name, online=True):
        self.agents[aid] = {"agent_id": aid, "name": name, "online": online, "last_seen": None, "info": {}}

    # ---- client API used by the router
    def send_message(self, text, channel="app", to_agent=None, context=None):
        self.n += 1
        self.msgs[self.n] = {"id": self.n, "text": text, "channel": channel, "to": to_agent, "context": context,
                             "delivered": False, "cancelled": False}
        return {"id": self.n}

    def sync(self, after_id):
        brain = [b for b in self.brain if b["id"] > after_id]
        pend = [m for m in self.msgs.values() if m["to"] and not m["delivered"] and not m["cancelled"]]
        return {"messages": brain, "last_id": brain[-1]["id"] if brain else after_id,
                "unrouted": [u for u in self.unrouted if u["to"] is None],
                "pending": [{"id": m["id"], "to_agent": m["to"], "channel": m["channel"], "age_s": 0} for m in pend],
                "agents": list(self.agents.values())}

    def route_message(self, mid, to, ctx):
        m = self.msgs[mid]; m["to"], m["context"] = to, ctx
        for u in self.unrouted:
            if u["id"] == mid: u["to"] = to

    def cancel_message(self, mid):
        m = self.msgs[mid]
        if m["delivered"]:
            return {"cancelled": False, "delivered": True}
        m["cancelled"] = True
        return {"cancelled": True, "delivered": False}

    # ---- test helpers
    def pickup(self, agent):
        got = [m for m in self.msgs.values() if m["to"] == agent and not m["delivered"] and not m["cancelled"]]
        for m in got: m["delivered"] = True
        return got

    def says(self, agent, channel, text, kind="msg"):
        self.n += 1
        self.brain.append({"id": self.n, "channel": channel, "text": text, "kind": kind, "from_agent": agent})


class Cfg:
    ack_timeout_s, stall_warn_s = 0.3, 0.6


class Eng:
    def __init__(self, relay):
        self._client, self.cfg, self.chat_rows, self.notes = relay, Cfg(), [], []

    def _add_chat(self, who, text, channel="app", agent=None): self.chat_rows.append((who, text, channel, agent))
    def _emit(self, *a): pass
    def notify(self, level, msg): self.notes.append(msg)


@pytest.fixture
def rig(tmp_path):
    relay = FakeRelay()
    relay.add_agent("nova", "Nova"); relay.add_agent("atlas", "Atlas")
    eng = Eng(relay)
    store = ChatStore(tmp_path / "chats.db")
    r = Router(eng, store); r.inline = True
    r.sync_once()
    return relay, eng, store, r


def sys_notes(store, ch):
    return [m["text"] for m in store.messages(ch) if m["who"] == "sys"]


def test_call_agents_by_name_and_sticky_least_loaded(rig):
    relay, eng, store, r = rig
    r.route_user_message("@atlas build the page", "app:a"); r.sync_once()
    assert relay.msgs[1]["to"] == "atlas" and store.chat("app:a")["assigned"] == "atlas"
    r.route_user_message("nova, check the logs", "app:b")
    assert relay.msgs[2]["to"] == "nova"
    r.route_user_message("a follow-up with no name", "app:a")
    assert relay.msgs[3]["to"] == "atlas"                                        # sticky
    r.route_user_message("bob, who is bob", "app:c")                            # unknown name = plain text, normal pick
    assert relay.msgs[4]["to"] in ("nova", "atlas")
    r.route_user_message("hi", "app:d"); r.route_user_message("hi", "app:e"); r.route_user_message("hi", "app:f")
    loads = [store.chat(c)["assigned"] for c in ("app:a", "app:b", "app:c", "app:d", "app:e", "app:f")]
    assert abs(loads.count("nova") - loads.count("atlas")) <= 1                  # work is balanced


def test_offline_agent_hands_chat_to_another_with_the_same_context(rig):
    relay, eng, store, r = rig
    store.ensure_chat("app:x", "Landing page")
    store.set_notes("app:x", "site v1 built; next: pricing section", "nova")
    store.add_event("app:x", "task", "wrote index.html", "nova")
    store.add_message("app:x", "you", "build me a landing page"); store.add_message("app:x", "brain", "on it", agent="nova")
    r.route_user_message("@nova add a pricing section", "app:x")
    r.sync_once()
    assert relay.msgs[1]["to"] == "nova"
    relay.agents["nova"]["online"] = False                                       # Nova dies before picking it up
    r.sync_once()
    assert relay.msgs[1]["cancelled"] is True                                    # retracted: she can never pick it up late
    new = relay.msgs[2]
    assert new["to"] == "atlas" and new["text"] == "@nova add a pricing section"
    ctx = new["context"]
    assert ctx["notes"] == "site v1 built; next: pricing section"                # SAME context
    assert [m["text"] for m in ctx["recent_messages"]][:2] == ["build me a landing page", "on it"]
    assert ctx["recent_events"][0]["text"] == "wrote index.html"
    assert ctx["handoff"]["from"] == "Nova" and ctx["handoff"]["to"] == "Atlas" and "offline" in ctx["handoff"]["reason"]
    assert any("Handed over to Atlas" in t for t in sys_notes(store, "app:x"))
    assert store.chat("app:x")["assigned"] == "atlas" and store.handoffs("app:x")[0]["to_agent"] == "Atlas"


def test_agent_that_never_picks_up_is_replaced_after_the_ack_timeout(rig):
    relay, eng, store, r = rig
    r.route_user_message("@nova hello", "app:x"); r.sync_once()
    assert relay.msgs[1]["to"] == "nova" and not relay.msgs[1]["cancelled"]
    time.sleep(0.4)
    r.sync_once()                                                                # Nova is online but silent
    assert relay.msgs[1]["cancelled"] and relay.msgs[2]["to"] == "atlas"
    assert relay.pickup("atlas") and r.pending[2]["to"] == "atlas"
    r.sync_once()
    assert 2 not in r.pending                                                    # picked up -> done, no more failover


def test_cancel_race_message_was_picked_up_just_in_time(rig):
    relay, eng, store, r = rig
    r.route_user_message("@nova hi", "app:x"); r.sync_once()
    time.sleep(0.4)
    orig = relay.sync
    relay.sync = lambda a: orig(a)                                               # router still sees it as pending...
    relay.msgs[1]["delivered"] = True                                            # ...but Nova grabbed it at that instant
    p = dict(r.pending[1]); r._failover(1, p, "x")
    assert len(relay.msgs) == 1                                                  # no duplicate dispatch


def test_agent_dies_after_picking_up_work_continuation_handoff(rig):
    relay, eng, store, r = rig
    r.route_user_message("@nova refactor the app", "app:x"); r.sync_once()
    relay.pickup("nova"); r.sync_once()                                          # delivered, Nova is working
    store.add_event("app:x", "task", "wrote src/App.jsx", "nova")
    relay.agents["nova"]["online"] = False
    r.sync_once()
    m = relay.msgs[2]
    assert m["to"] == "atlas" and "[handoff]" in m["text"] and "refactor the app" in m["text"]
    assert m["context"]["handoff"]["continuation"] is True
    assert any(e["text"] == "wrote src/App.jsx" for e in m["context"]["recent_events"])   # Atlas can see what Nova already did


def test_no_agent_online_queues_then_delivers_when_one_connects(rig):
    relay, eng, store, r = rig
    for a in relay.agents.values(): a["online"] = False
    r.sync_once()
    assert r.route_user_message("anyone there?", "app:x")
    assert not relay.msgs and any("No agent is online" in t for t in sys_notes(store, "app:x"))
    relay.agents["atlas"]["online"] = True
    r.sync_once()
    assert relay.msgs[1]["to"] == "atlas" and relay.msgs[1]["text"] == "anyone there?"
    r.sync_once(); assert not r.waiting


def test_stop_is_broadcast_to_every_agent(rig):
    relay, eng, store, r = rig
    r.route_user_message("STOP", "app:x")
    assert sorted(m["to"] for m in relay.msgs.values()) == ["atlas", "nova"]
    assert all(m["context"]["stop"] for m in relay.msgs.values())


def test_notes_are_chat_memory_and_replies_are_deduped(rig):
    relay, eng, store, r = rig
    relay.says("nova", "app:x", "hello user")
    relay.says("nova", "app:x", "site v1 done; next pricing", kind="notes")
    r.sync_once(); r.sync_once()
    assert [m["text"] for m in store.messages("app:x") if m["who"] == "brain"] == ["hello user"]
    assert store.chat("app:x")["notes"] == "site v1 done; next pricing" and store.chat("app:x")["assigned"] == "nova"
    assert eng.chat_rows[0] == ("brain", "hello user", "app:x", "nova")           # shown in the app, with the agent's name
    r2 = Router(eng, store); assert r2.after_id == r.after_id                    # survives an app restart


def test_relay_origin_messages_are_routed_by_the_laptop(rig):
    relay, eng, store, r = rig
    relay.n += 1
    relay.msgs[relay.n] = {"id": relay.n, "text": "@atlas hi from the API", "channel": "relay:z", "to": None, "context": None,
                           "delivered": False, "cancelled": False}
    relay.unrouted.append({"id": relay.n, "channel": "relay:z", "text": "@atlas hi from the API", "to": None})
    r.sync_once(); r.sync_once()
    assert relay.msgs[relay.n]["to"] == "atlas" and relay.msgs[relay.n]["context"]["you_are"]["name"] == "Atlas"
    assert [m["text"] for m in store.messages("relay:z")] == ["@atlas hi from the API"]          # context kept ON THE LAPTOP


def test_gives_up_after_max_handoffs_and_pin_beats_sticky(rig):
    relay, eng, store, r = rig
    r.pin("app:p", "atlas")
    r.route_user_message("hi", "app:p")
    assert relay.msgs[1]["to"] == "atlas"
    store.set_assigned("app:p", "nova")
    r.route_user_message("hi again", "app:p")
    assert relay.msgs[2]["to"] == "atlas"                                         # pinned wins over sticky
    r._handover("app:p", "x", 1, "nova", "failed", 4, {"nova"}, False)
    assert any("none could take this" in t for t in sys_notes(store, "app:p"))


def test_reassign_moves_open_work_now(rig):
    relay, eng, store, r = rig
    r.route_user_message("@nova long job", "app:x"); r.sync_once()
    relay.pickup("nova"); r.sync_once()
    r.reassign("app:x")
    assert relay.msgs[2]["to"] == "atlas" and "reassigned by you" in relay.msgs[2]["context"]["handoff"]["reason"]


def test_classic_single_brain_still_works_until_an_agent_registers(tmp_path):
    relay = FakeRelay()                       # NO agent has ever registered (a Brain that does not call /agents/hello)
    eng = Eng(relay); store = ChatStore(tmp_path / "c.db")
    r = Router(eng, store); r.inline = True; r.sync_once()
    assert r.route_user_message("hello brain", "app")
    assert relay.msgs[1]["to"] is None and relay.msgs[1]["text"] == "hello brain"        # unaddressed: a classic Brain can read it
    assert not any("No agent is online" in m["text"] for m in store.messages("app"))
    r.route_user_message("stop", "app")
    assert relay.msgs[2]["to"] is None and relay.msgs[2]["text"] == "stop"
    relay.add_agent("nova", "Nova"); r.sync_once()                                         # first agent registers -> multi-agent mode
    r.route_user_message("now addressed", "app")
    assert relay.msgs[3]["to"] == "nova"
