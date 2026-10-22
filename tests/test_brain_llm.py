"""The Claude path, with the API faked: tool calls must be executed, proposals collected, nothing written."""
from types import SimpleNamespace as NS


class FakeMessages:
    def __init__(self, script):
        self.script, self.calls = script, []

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})  # snapshot: the loop appends in place
        return self.script.pop(0)


def test_claude_tool_loop(api, monkeypatch):
    import anthropic
    from app import brain, db

    c = api.post("/api/clients", json={"name": "Sharma Traders", "gstin": "27ABCPS1234K1Z5"}, headers=api.partner).json()
    script = [
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="find_clients", input={"query": "sharma"})]),
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t2", name="propose_due_override",
                                                input={"rule_code": "GSTR9", "fy": 2026, "period_key": "*",
                                                       "new_due": "2028-01-31", "reason": "test"})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="Drafted the extension for approval.")]),
    ]
    fake = FakeMessages(script)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: NS(beta=NS(messages=fake)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CABOT_LLM", "1")
    r = api.post("/api/ask", json={"text": "can you push sharma's annual GST return a month?"}, headers=api.staff).json()
    assert r["via"] == "claude" and r["answer"].startswith("Drafted")
    assert r["proposals"][0]["kind"] == "override_due"
    first = fake.calls[0]
    assert first["model"] == brain.MODEL and first["fallbacks"] == "default"
    # tool result for find_clients was fed back
    assert "Sharma Traders" in fake.calls[1]["messages"][-1]["content"][0]["content"]
    with db.tx() as conn:
        assert db.rows(conn, "SELECT * FROM due_overrides") == []  # proposal only, nothing applied
    assert c["id"]
