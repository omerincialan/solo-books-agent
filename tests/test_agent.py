import pytest
from solobooks import agent, books
from tests.fakes import FakeSheet, ScriptedModel


@pytest.fixture(autouse=True)
def fake_sheet(monkeypatch):
    sheet = FakeSheet()
    monkeypatch.setattr(books, "_sheet", sheet)
    monkeypatch.setattr(books, "_tabs", {})
    monkeypatch.setattr(agent, "today", lambda: "2026-10-28")
    return sheet


def use_model(monkeypatch, steps):
    model = ScriptedModel(steps)
    monkeypatch.setattr(agent, "client", model)
    return model


def test_one_message_records_three_entries(monkeypatch, fake_sheet):
    model = use_model(monkeypatch, [
        [("record_trip", {"date": "2026-10-28", "miles": 12, "destination": "Henderson job", "purpose": "oak removal"}),
         ("record_expense", {"date": "2026-10-28", "vendor": "Ace Hardware", "total": 23, "category": "Equipment & parts", "notes": "chain oil"}),
         ("record_income", {"date": "2026-10-28", "payer": "Hendersons", "amount": 400, "paid_with": "cash"})],
        lambda results: "✅ Recorded 3 entries: " + ", ".join(r["id"] for r in results),
    ])
    reply = agent.run("drove 12 miles to the Hendersons, bought chain oil for 23 bucks, they paid 400 cash", source="voice")

    assert len(fake_sheet.tabs["Mileage"].rows) == 2   # header + 1
    assert len(fake_sheet.tabs["Expenses"].rows) == 2
    assert len(fake_sheet.tabs["Income"].rows) == 2
    trip = books.entries("trip")[0]
    assert trip["deduction"] == 8.4 and trip["rate"] == 0.70 and trip["source"] == "voice"
    assert reply.startswith("✅ Recorded 3 entries: T")
    # The tool results went back to the model, one per call
    assert [m["role"] for m in model.requests[1]["messages"][-3:]] == ["tool", "tool", "tool"]


def test_totals_are_exact_and_computed_in_code(monkeypatch):
    books.add("income", {"date": "2026-10-05", "payer": "Lee", "amount": 1200})
    books.add("income", {"date": "2026-10-20", "payer": "Ortiz", "amount": 650.5})
    books.add("income", {"date": "2026-09-30", "payer": "Old job", "amount": 999})       # outside October
    books.add("expense", {"date": "2026-10-07", "vendor": "Shell", "total": 64.1, "category": "Fuel"})
    books.add("expense", {"date": "2026-10-12", "vendor": "Shell", "total": 58.9, "category": "Fuel"})
    books.add("expense", {"date": "2026-10-15", "vendor": "Dump", "total": 45, "category": "Dump & disposal fees"})
    books.add("trip", {"date": "2026-10-20", "miles": 30, "destination": "Ortiz"})

    totals = agent.get_totals("2026-10-01", "2026-10-31", _ctx={})
    assert totals["income"] == 1850.5
    assert totals["expenses"] == 168.0
    assert totals["expenses_by_category"] == {"Fuel": 123.0, "Dump & disposal fees": 45.0}
    assert totals["profit"] == 1682.5
    assert totals["business_miles"] == 30 and totals["mileage_deduction"] == 21.0


def test_question_uses_get_totals(monkeypatch):
    books.add("income", {"date": "2026-10-05", "payer": "Lee", "amount": 1200})
    model = use_model(monkeypatch, [
        [("get_totals", {"start_date": "2026-10-01", "end_date": "2026-10-31"})],
        lambda results: f"October profit: ${results[0]['profit']:.2f}",
    ])
    assert agent.run("how much profit this month?") == "October profit: $1200.00"
    # The model was offered every tool, and the system prompt lists the trade's categories
    names = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert names == set(agent.TOOLS)
    assert "Dump & disposal fees" in model.requests[0]["messages"][0]["content"]


def test_fix_and_delete_by_id(monkeypatch):
    entry = books.add("expense", {"date": "2026-10-28", "vendor": "Shell", "total": 6.41, "category": "Fuel"})
    use_model(monkeypatch, [[("update_entry", {"entry_id": entry["id"], "changes": {"total": 64.1}})], "✏️ Fixed"])
    agent.run("that was 64.10 not 6.41")
    assert books.entries("expense")[0]["total"] == 64.1

    use_model(monkeypatch, [[("delete_entry", {"entry_id": entry["id"]})], "🗑️ Deleted"])
    agent.run("delete it")
    assert books.entries("expense") == []


def test_recent_entries_are_in_the_prompt_so_fix_it_works(monkeypatch):
    entry = books.add("trip", {"date": "2026-10-28", "miles": 7, "destination": "Lowe's"})
    model = use_model(monkeypatch, ["ok"])
    agent.run("make that 14 miles")
    assert entry["id"] in model.requests[0]["messages"][0]["content"]


def test_photo_is_sent_to_the_model_and_linked_on_the_expense(monkeypatch):
    model = use_model(monkeypatch, [
        [("record_expense", {"date": "2026-10-28", "vendor": "Home Depot", "total": 41.2, "category": "Safety gear"})],
        "✅ Recorded",
    ])
    agent.run("", images=["data:image/jpeg;base64,AAAA"], source="photo", photo="http://x/photos/abc.jpg")
    user_content = model.requests[0]["messages"][-1]["content"]
    assert user_content[1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
    expense = books.entries("expense")[0]
    assert expense["photo"] == "http://x/photos/abc.jpg" and expense["source"] == "photo"


def test_bad_tool_arguments_go_back_to_the_model_as_errors(monkeypatch):
    model = use_model(monkeypatch, [[("update_entry", {"entry_id": "E0000", "changes": {"total": 1}})], "Sorry"])
    agent.run("fix E0000")
    assert "No entry with ID E0000" in model.requests[1]["messages"][-1]["content"]


def test_values_are_written_raw_never_as_formulas(fake_sheet):
    books.add("expense", {"date": "2026-10-28", "vendor": "=IMPORTXML(\"evil\")", "total": 1, "category": "Other"})
    assert fake_sheet.tabs["Expenses"].rows[1][3] == "=IMPORTXML(\"evil\")"
