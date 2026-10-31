import pytest
from solobooks import agent, books, clients, config, invoices, memory, outbox, server
from tests.fakes import FakeSheet, ScriptedModel
from tests.test_server import SyncThread, post

OWNER = "+15550001111"
TODAY = "2026-10-31"


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(books, "_sheet", FakeSheet())
    monkeypatch.setattr(books, "_tabs", {})
    monkeypatch.setattr(agent, "today", lambda: TODAY)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "BUSINESS_NAME", "Denali Tree Care")


@pytest.fixture
def sent(monkeypatch):
    texts = []
    monkeypatch.setattr(server, "send_text", lambda to, body, media_url=None: texts.append((to, body)))
    monkeypatch.setattr(server.threading, "Thread", SyncThread)
    return texts


def invoice_for(name="Mike Henderson", phone="(650) 555-0142"):
    clients.add(name, phone=phone)
    return invoices.create(name, [{"description": "Oak removal", "unit_price": 690}], TODAY)


@pytest.mark.parametrize("raw, expected", [("(650) 555-0142", "+16505550142"), ("1-650-555-0142", "+16505550142"),
                                           ("+44 20 7946 0958", "+442079460958"), ("555-0142", None), (None, None)])
def test_phone_numbers(raw, expected):
    assert outbox.e164(raw) == expected


def test_prepare_never_sends_and_builds_a_compliant_message(sent):
    inv = invoice_for()
    preview = outbox.prepare(OWNER, inv["id"])
    assert preview["not_sent_yet"] and preview["to"] == "+16505550142" and sent == []
    body = preview["message"]
    assert body.startswith("Hi Mike, this is Denali Tree Care. Your invoice INV-0001 for $690.00")
    assert inv["link"] in body and body.endswith("Reply STOP to opt out.")
    custom = outbox.prepare(OWNER, inv["id"], message="Friendly reminder: the oak job invoice is still open.")
    assert custom["message"].startswith("Denali Tree Care: Friendly reminder") and inv["link"] in custom["message"]


def test_missing_phone_is_asked_for_and_then_remembered(sent):
    clients.add("Kim Lee")
    inv = invoices.create("Kim Lee", [{"description": "Trim", "unit_price": 100}], TODAY)
    assert "No usable phone number" in outbox.prepare(OWNER, inv["id"])["error"]
    assert outbox.prepare(OWNER, inv["id"], phone="650 555 0199")["to"] == "+16505550199"
    assert clients.find("Kim Lee")[0]["phone"] == "650 555 0199"


def test_only_the_owners_next_yes_sends_it(sent):
    inv = invoice_for()
    outbox.prepare(OWNER, inv["id"])
    c = server.app.test_client()
    post(c, {"From": OWNER, "Body": "Yes!", "NumMedia": "0"})
    assert sent[0][0] == "+16505550142" and "INV-0001" in sent[0][1]                       # to the client
    assert sent[1] == (OWNER, "📤 Sent INV-0001 to Mike Henderson (+16505550142).")         # confirmation
    post(c, {"From": OWNER, "Body": "yes", "NumMedia": "0"})                                # nothing pending now
    assert all(to != "+16505550142" for to, _ in sent[2:])


def test_no_cancels_and_other_messages_cancel_too(sent, monkeypatch):
    inv = invoice_for()
    c = server.app.test_client()
    outbox.prepare(OWNER, inv["id"])
    post(c, {"From": OWNER, "Body": "no", "NumMedia": "0"})
    assert sent == [(OWNER, "OK, not sent.")]
    outbox.prepare(OWNER, inv["id"])
    monkeypatch.setattr(server.agent, "run", lambda text, **k: "handled normally")
    post(c, {"From": OWNER, "Body": "how much profit this month?", "NumMedia": "0"})
    post(c, {"From": OWNER, "Body": "yes", "NumMedia": "0"})                                # too late: cancelled
    assert not any(to == "+16505550142" for to, _ in sent)


def test_an_old_yes_expires(sent, monkeypatch):
    inv = invoice_for()
    outbox.prepare(OWNER, inv["id"])
    original = memory.take_pending
    monkeypatch.setattr(server.memory, "take_pending", lambda owner: original(owner, max_age_minutes=-1))
    post(server.app.test_client(), {"From": OWNER, "Body": "yes", "NumMedia": "0"})
    assert sent == [(OWNER, "⌛ That send request expired. Ask me again and I'll prepare it.")]


def test_client_replies_are_forwarded_with_their_name(sent):
    invoice_for()
    post(server.app.test_client(), {"From": "+16505550142", "Body": "Paid, thanks!", "NumMedia": "0"})
    assert sent == [(OWNER, "💬 Mike Henderson (+16505550142): Paid, thanks!")]


def test_agent_prepares_but_cannot_send(monkeypatch, sent):
    inv = invoice_for()
    model = ScriptedModel([[("text_client", {"invoice_id": inv["id"]})],
                           lambda r: f"To {r[0]['to']}: {r[0]['message']} Reply YES to send or NO to cancel."])
    monkeypatch.setattr(agent, "client", model)
    reply = agent.run("text the Hendersons their invoice")
    assert "Reply YES to send" in reply and sent == [] and memory.has_pending(config.OWNER_PHONE_NUMBER)
    assert "only when the owner replies YES" in model.requests[0]["messages"][0]["content"]
