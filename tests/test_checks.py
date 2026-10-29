import pytest
from solobooks import agent, books, checks
from tests.fakes import FakeSheet, ScriptedModel

TODAY = "2026-10-28"


@pytest.fixture(autouse=True)
def fake_sheet(monkeypatch):
    monkeypatch.setattr(books, "_sheet", FakeSheet())
    monkeypatch.setattr(books, "_tabs", {})
    monkeypatch.setattr(agent, "today", lambda: TODAY)


def expense(**kw):
    return {"kind": "expense", "id": "E0001", "date": "2026-10-12", "vendor": "Shell", "total": 64.1,
            "category": "Fuel", **kw}


@pytest.mark.parametrize("new, dup", [
    ({"date": "2026-10-12", "vendor": "Shell", "total": 64.10}, True),            # same receipt again
    ({"date": "2026-10-12", "vendor": "SHELL #5521", "total": 64.1}, True),       # vendor spelled differently
    ({"date": "2026-10-14", "vendor": "Shell", "total": 64.1}, True),             # same vendor + amount, 2 days later
    ({"date": "2026-10-12", "vendor": "Chevron", "total": 64.1}, True),           # same day + same total
    ({"date": "2026-10-20", "vendor": "Shell", "total": 64.1}, False),            # weekly fill-up, a week later
    ({"date": "2026-10-12", "vendor": "Shell", "total": 58.9}, False),            # different amount
])
def test_expense_duplicates(new, dup):
    assert checks.is_duplicate("expense", new, expense()) is dup


@pytest.mark.parametrize("new, expect", [
    ({"date": TODAY, "vendor": "Cafe", "total": 4520, "category": "Meals"}, "unusually high"),
    ({"date": TODAY, "vendor": "Cafe", "total": 0, "category": "Meals"}, "isn't positive"),
    ({"date": TODAY, "vendor": "Ace", "total": 20, "tax": 25, "category": "Other"}, "tax"),
    ({"date": TODAY, "vendor": "Ace", "total": 20, "tax": 6, "category": "Other"}, "more than 20%"),
    ({"date": "2026-12-25", "vendor": "Ace", "total": 20, "category": "Other"}, "future"),
    ({"date": "2024-01-02", "vendor": "Ace", "total": 20, "category": "Other"}, "more than a year ago"),
])
def test_implausible_expenses_are_flagged(new, expect):
    assert any(expect in p for p in checks.problems("expense", new, [], TODAY))


def test_normal_entries_pass():
    assert checks.problems("expense", {"date": TODAY, "vendor": "Shell", "total": 64.1, "tax": 4.9}, [], TODAY) == []
    assert checks.problems("trip", {"date": TODAY, "miles": 24, "destination": "Lee job"}, [], TODAY) == []
    assert checks.problems("income", {"date": TODAY, "payer": "Lee", "amount": 1200}, [], TODAY) == []
    assert any("unusually high" in p for p in checks.problems("trip", {"date": TODAY, "miles": 900, "destination": "x"}, [], TODAY))


def test_duplicate_is_not_recorded_until_confirmed():
    shell = dict(date=TODAY, vendor="Shell", total=64.1, category="Fuel")
    first = agent.record_expense(**shell, _ctx={})
    again = agent.record_expense(**shell, _ctx={})
    assert again["not_recorded"] and first["id"] in again["needs_confirmation"][0]
    assert len(books.entries("expense")) == 1
    confirmed = agent.record_expense(**shell, confirm=True, _ctx={})
    assert confirmed["id"] and len(books.entries("expense")) == 2


def test_totals_point_out_entries_recorded_twice():
    a = books.add("expense", {"date": "2026-10-12", "vendor": "Shell", "total": 64.1, "category": "Fuel"})
    b = books.add("expense", {"date": "2026-10-12", "vendor": "Shell", "total": 64.1, "category": "Fuel"})
    books.add("expense", {"date": "2026-10-20", "vendor": "Shell", "total": 64.1, "category": "Fuel"})
    totals = agent.get_totals("2026-10-01", "2026-10-31", _ctx={})
    assert totals["possible_duplicates"] == [[a["id"], b["id"], "Shell $64.1 (Fuel)"]]
    assert totals["expenses"] == 192.3   # totals still count everything; the agent asks before deleting


def test_one_message_records_the_good_entries_and_asks_about_the_doubtful_one(monkeypatch):
    old = books.add("expense", {"date": TODAY, "vendor": "Shell", "total": 64.1, "category": "Fuel"})
    model = ScriptedModel([
        [("record_trip", {"date": TODAY, "miles": 12, "destination": "Henderson job"}),
         ("record_expense", {"date": TODAY, "vendor": "Shell", "total": 64.1, "category": "Fuel"})],
        lambda results: "✅ Trip recorded. Looks like Shell $64.10 is already in. Record it again?",
    ])
    monkeypatch.setattr(agent, "client", model)
    agent.run("drove 12 miles to the Henderson job, and gas at Shell 64.10")
    assert len(books.entries("trip")) == 1 and len(books.entries("expense")) == 1
    tool_results = [m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool"]
    assert "needs_confirmation" in tool_results[1] and old["id"] in tool_results[1]
    # The confirm flag is offered to the model on every record tool
    tools = {t["function"]["name"]: t["function"]["parameters"]["properties"] for t in model.requests[0]["tools"]}
    assert all("confirm" in tools[name] for name in ("record_expense", "record_income", "record_trip"))
