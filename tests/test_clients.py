import pytest
from solobooks import agent, books, clients, config, invoices
from tests.fakes import FakeSheet, ScriptedModel

TODAY = "2026-10-29"


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(books, "_sheet", FakeSheet())
    monkeypatch.setattr(books, "_tabs", {})
    monkeypatch.setattr(agent, "today", lambda: TODAY)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))


@pytest.mark.parametrize("query, name, expected", [
    ("the Hendersons", "Mike Henderson", True),
    ("Henderson", "Mike Henderson", True),
    ("Henderson's", "Mike Henderson", True),
    ("Mike Henderson", "Henderson family", True),
    ("Mike", "Mike Henderson", True),
    ("Sara Henderson", "Mike Henderson", False),
    ("Lee", "Kim Lee", True),
    ("Leeds", "Kim Lee", False),
    ("Gus", "Gus Bass", True),
])
def test_name_matching(query, name, expected):
    assert clients.matches(query, name) is expected


def test_add_find_and_duplicate_check():
    mike = clients.add("Mike Henderson", phone="(650) 555-0142", email="Mike@Example.com", address="12 Oak St")
    assert mike["id"].startswith("C") and mike["email"] == "mike@example.com"
    again = clients.add("The Hendersons")
    assert again["not_added"] and again["possible_existing"][0]["id"] == mike["id"]
    same_phone = clients.add("M. H.", phone="650-555-0142")
    assert same_phone["not_added"]
    assert clients.add("Sara Henderson", confirm=True)["name"] == "Sara Henderson"
    assert [c["name"] for c in clients.find("hendersons")] == ["Mike Henderson", "Sara Henderson"]
    assert [c["name"] for c in clients.find("Mike Henderson")] == ["Mike Henderson"]
    assert [c["name"] for c in clients.find("mike@example.com")] == ["Mike Henderson"]
    with pytest.raises(ValueError):
        clients.add("Bad Email", email="not-an-email")


def test_notes_are_appended_not_overwritten():
    c = clients.add("Kim Lee", notes="Prefers mornings")
    updated = clients.update(c["id"], {}, append_note="Gate code 4471, dog in yard")
    assert updated["notes"].startswith("Prefers mornings; Gate code 4471, dog in yard (")
    assert clients.update(c["id"], {"phone": "650 555 0199"})["phone"] == "650 555 0199"
    assert clients.update("C0000", {"phone": "1"}) is None


def test_client_summary_numbers_are_exact():
    mike = clients.add("Mike Henderson")
    a = invoices.create("Mike Henderson", [{"description": "Oak removal", "quantity": 6, "unit_price": 95}], "2026-09-01",
                        due_date="2026-09-15")
    invoices.mark_paid(a["id"], "check", "2026-09-10", "text")                                 # paid in 9 days
    invoices.create("Mike Henderson", [{"description": "Hedge trim", "unit_price": 240}], "2026-10-20",
                    due_date="2026-10-27")                                                       # 2 days overdue
    books.add("income", {"date": "2026-10-02", "payer": "the Hendersons", "amount": 50, "paid_with": "cash"})
    s = clients.summary(mike, TODAY)
    assert (s["invoices"], s["billed"], s["owed"]) == (2, 810.0, 240.0)
    assert s["received"] == 620.0                          # 570 by check + 50 cash
    assert s["average_days_to_pay"] == 9.0 and s["last_activity"] == "2026-10-20"
    assert s["open_invoices"] == [{"id": "INV-0002", "total": 240.0, "due": "2026-10-27", "days_overdue": 2}]


def test_invoice_uses_the_known_client_and_creates_new_ones():
    clients.add("Mike Henderson", email="mike@example.com")
    result = agent.create_invoice("the Hendersons", [{"description": "Oak removal", "unit_price": 690}], _ctx={})
    assert result["client"] == "Mike Henderson" and result["client_email"] == "mike@example.com"
    new = agent.create_invoice("Ortiz", [{"description": "Trim", "unit_price": 300}], _ctx={"source": "voice"})
    assert new["client"] == "Ortiz" and [c["name"] for c in books.entries("client")] == ["Mike Henderson", "Ortiz"]


def test_ambiguous_client_is_never_guessed():
    clients.add("Kim Lee")
    clients.add("Sara Lee", confirm=True)
    result = agent.create_invoice("Lee", [{"description": "Trim", "unit_price": 100}], _ctx={})
    assert result["not_created"] and {c["name"] for c in result["ambiguous_client"]} == {"Kim Lee", "Sara Lee"}
    assert books.entries("invoice") == []
    exact = agent.create_invoice("Sara Lee", [{"description": "Trim", "unit_price": 100}], _ctx={})
    assert exact["client"] == "Sara Lee"


def test_agent_adds_a_client_from_a_business_card_and_answers_about_them(monkeypatch):
    model = ScriptedModel([
        [("add_client", {"name": "Mike Henderson", "phone": "650-555-0142", "email": "mike@example.com",
                         "address": "12 Oak St, Palo Alto"})],
        lambda results: f"👤 Added {results[0]['name']} ({results[0]['id']})",
    ])
    monkeypatch.setattr(agent, "client", model)
    reply = agent.run("", images=["data:image/jpeg;base64,AAAA"], source="photo", photos=["http://x/p.jpg"])
    assert reply.startswith("👤 Added Mike Henderson (C") and books.entries("client")[0]["source"] == "photo"
    assert "business card" in model.requests[0]["messages"][0]["content"]

    monkeypatch.setattr(agent, "client", ScriptedModel([
        [("find_clients", {"query": "Hendersons"})],
        lambda results: f"{results[0]['clients'][0]['client']['address']}",
    ]))
    assert agent.run("what's the Hendersons' address?") == "12 Oak St, Palo Alto"
    tools = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert {"add_client", "update_client", "find_clients"} <= tools
