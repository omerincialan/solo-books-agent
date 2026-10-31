import pytest
from solobooks import agent, books, config, crew
from tests.fakes import FakeSheet, ScriptedModel

TODAY = "2026-10-31"


@pytest.fixture(autouse=True)
def sheet(monkeypatch, tmp_path):
    fake = FakeSheet()
    monkeypatch.setattr(books, "_sheet", fake)
    monkeypatch.setattr(books, "_tabs", {})
    monkeypatch.setattr(agent, "today", lambda: TODAY)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    for name, rate in (("Diego", 25), ("Jonny", 22), ("Alex", None)):
        crew.add_worker(name, rate=rate)
    return fake


def grid(sheet):
    """The tab as Google Sheets returns it: every row padded to the header's width."""
    rows = sheet.tabs["Crew Hours"].rows
    width = len(rows[0])
    return [r + [""] * (width - len(r)) for r in rows]


def test_the_grid_looks_like_a_paper_timesheet(sheet):
    crew.log_hours("2026-10-30", [{"name": "Diego", "hours": 4}, {"name": "Jonny", "hours": 4}])
    crew.log_hours("2026-10-31", [{"name": "Alex", "hours": 6}])
    assert grid(sheet) == [["Date", "Diego", "Jonny", "Alex"],
                           ["2026-10-30", "4", "4", ""],
                           ["2026-10-31", "", "", "6"]]
    # Hours are sent as numbers, not text, so SUM() works in the sheet
    assert sheet.tabs["Crew Hours"].raw[-1] == ["2026-10-31", "", "", 6]


def test_two_jobs_in_a_day_add_up_and_corrections_replace(sheet):
    crew.log_hours(TODAY, [{"name": "Diego", "hours": 4}])
    crew.log_hours(TODAY, [{"name": "diego", "hours": 2.5}])                 # second job, any capitalization
    assert grid(sheet)[1][:2] == [TODAY, "6.5"]
    result = crew.log_hours(TODAY, [{"name": "Diego", "hours": 3}], mode="set")
    assert grid(sheet)[1][:2] == [TODAY, "3"] and result["hours"] == {"Diego": 3}


def test_unknown_or_impossible_entries_are_not_recorded(sheet):
    result = crew.log_hours(TODAY, [{"name": "Jony", "hours": 4}, {"name": "Diego", "hours": 30}])
    assert result["not_recorded"]
    problems = {p["name"]: p["problem"] for p in result["problems"]}
    assert problems == {"Jony": "not on the crew list yet", "Diego": "30 hours isn't possible in one day"}
    assert "Crew Hours" not in sheet.tabs or len(grid(sheet)) <= 1
    added = crew.log_hours(TODAY, [{"name": "Marco", "hours": 5}], confirm_new=True)
    assert added["hours"] == {"Marco": 5} and [w["name"] for w in crew.workers()][-1] == "Marco"
    with pytest.raises(ValueError):
        crew.log_hours("yesterday", [{"name": "Diego", "hours": 1}])


def test_ambiguous_names_are_asked_about(sheet):
    crew.add_worker("Alex Kim", confirm=True)
    result = crew.log_hours(TODAY, [{"name": "Alex", "hours": 2}])
    assert result["hours"] == {"Alex": 2}                                    # exact name wins
    crew.add_worker("Jonny Ray", confirm=True)
    result = crew.log_hours(TODAY, [{"name": "Ray", "hours": 2}])
    assert result["hours"]["Jonny Ray"] == 2                                 # only one "Ray"
    crew.add_worker("Sam Ray", confirm=True)
    ambiguous = crew.log_hours(TODAY, [{"name": "Ray", "hours": 2}])
    assert ambiguous["not_recorded"] and set(ambiguous["problems"][0]["candidates"]) == {"Jonny Ray", "Sam Ray"}


def test_hours_report_totals_and_pay(sheet):
    crew.log_hours("2026-10-27", [{"name": "Diego", "hours": 8}, {"name": "Jonny", "hours": 8}])
    crew.log_hours("2026-10-29", [{"name": "Diego", "hours": 4}])
    crew.log_hours("2026-10-31", [{"name": "Alex", "hours": 6}])
    crew.log_hours("2026-10-20", [{"name": "Diego", "hours": 10}])           # outside the week
    report = crew.hours_report("2026-10-26", "2026-11-01")
    assert report["people"]["Diego"] == {"hours": 12.0, "days": 2, "rate": 25.0, "pay": 300.0}
    assert report["people"]["Jonny"]["pay"] == 176.0
    assert report["people"]["Alex"]["pay"] is None and report["missing_rates"] == ["Alex"]
    assert report["total_hours"] == 26.0 and report["total_pay"] == 476.0


def test_owner_can_reorder_rows_and_it_still_finds_the_date(sheet):
    crew.log_hours("2026-10-30", [{"name": "Diego", "hours": 4}])
    crew.log_hours("2026-10-31", [{"name": "Diego", "hours": 5}])
    ws = sheet.tabs["Crew Hours"]
    ws.rows[1], ws.rows[2] = ws.rows[2], ws.rows[1]                          # owner sorted newest first
    crew.log_hours("2026-10-30", [{"name": "Diego", "hours": 1}])
    assert [r[:2] for r in grid(sheet)[1:]] == [["2026-10-31", "5"], ["2026-10-30", "5"]]


def test_agent_logs_crew_hours_from_one_message(monkeypatch, sheet):
    model = ScriptedModel([
        [("log_hours", {"date": TODAY, "workers": [{"name": "Diego", "hours": 4}, {"name": "Jonny", "hours": 4}]})],
        lambda results: "👷 Logged Diego 4h, Jonny 4h",
    ])
    monkeypatch.setattr(agent, "client", model)
    agent.run("Diego and Jonny worked 4 hours today on the Henderson job")
    assert grid(sheet)[1][:3] == [TODAY, "4", "4"]
    tools = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert {"log_hours", "crew_hours", "add_worker"} <= tools
    assert "Crew wages" in model.requests[0]["messages"][0]["content"]
