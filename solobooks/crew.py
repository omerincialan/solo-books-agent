"""
Crew hours: a "Crew Hours" tab laid out like a paper timesheet, one row per date and one
column per person, plus a "Crew" tab with each person's hourly rate and phone.

             Diego   Jonny   Alex
2026-10-30   4       4
2026-10-31                   6

Rows are found by their date and columns by the person's name, so the owner can sort or
format this tab freely. Totals and pay are computed in code.
"""
import datetime
import gspread
from gspread.utils import rowcol_to_a1
from . import books, clients

HOURS_TAB = "Crew Hours"


def _hours_sheet():
    sheet = books.spreadsheet()
    try:
        return sheet.worksheet(HOURS_TAB)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(HOURS_TAB, rows=400, cols=10)
        ws.append_row(["Date"])
        return ws


def _number(value):
    try:
        return float(str(value).strip()) if str(value).strip() else 0.0
    except ValueError:
        return 0.0


def _as_number(value):
    if value in ("", None):
        return ""
    number = _number(value)
    return int(number) if number == int(number) else number


def workers() -> list:
    return books.entries("worker")


def resolve(name: str):
    """(worker, None) for one match, (None, candidates) when ambiguous, (None, []) when unknown."""
    found = [w for w in workers() if clients.matches(name, w.get("name"))]
    exact = [w for w in found if (w.get("name") or "").lower() == (name or "").strip().lower()]
    if len(exact) == 1 or len(found) == 1:
        return (exact or found)[0], None
    return None, found


def add_worker(name: str, rate=None, phone=None, notes=None, confirm=False):
    found = [w for w in workers() if clients.matches(name, w.get("name"))]
    if found and not confirm:
        return {"not_added": True, "possible_existing": found,
                "hint": "Ask whether this is the same person (then update_entry) or someone new (confirm=true)"}
    return books.add("worker", {"name": name.strip(), "rate": rate, "phone": phone, "notes": notes})


def log_hours(date: str, entries: list, mode: str = "add", confirm_new: bool = False) -> dict:
    """
    Records hours for one date. entries: [{"name", "hours"}]. mode "add" adds to what is already
    there (two jobs in a day); "set" replaces it (a correction). Unknown or ambiguous names are
    not recorded unless confirm_new (then a new person is added to the crew).
    """
    datetime.date.fromisoformat(date)  # rejects anything that isn't YYYY-MM-DD
    resolved, problems = [], []
    for entry in entries:
        hours = float(entry["hours"])
        if hours < 0 or hours > 24:
            problems.append({"name": entry["name"], "problem": f"{hours:g} hours isn't possible in one day"})
            continue
        worker, candidates = resolve(entry["name"])
        if worker is None and candidates:
            problems.append({"name": entry["name"], "problem": "ambiguous",
                             "candidates": [c["name"] for c in candidates]})
        elif worker is None and not confirm_new:
            problems.append({"name": entry["name"], "problem": "not on the crew list yet",
                             "known": [w["name"] for w in workers()]})
        else:
            if worker is None:
                worker = books.add("worker", {"name": entry["name"].strip()})
            resolved.append((worker["name"], hours))
    if problems:
        return {"not_recorded": True, "problems": problems,
                "hint": "Ask the owner. For a new person, call again with confirm_new=true"}

    ws = _hours_sheet()
    grid = ws.get_all_values() or [["Date"]]
    header = list(grid[0])
    for name, _ in resolved:
        if name not in header:
            header.append(name)
    if len(header) > ws.col_count:
        ws.add_cols(len(header) - ws.col_count + 5)
    if header != grid[0]:
        ws.update([header], f"A1:{rowcol_to_a1(1, len(header))}", value_input_option="RAW")

    row_number = next((i for i, row in enumerate(grid[1:], start=2) if row and row[0] == date), None)
    row = list(grid[row_number - 1]) if row_number else [date]
    row += [""] * (len(header) - len(row))
    for name, hours in resolved:
        col = header.index(name)
        row[col] = round((_number(row[col]) if mode == "add" else 0) + hours, 2)
    # Hours go in as numbers (not text) so the owner's own SUM formulas work on this tab
    row = [row[0]] + [_as_number(v) for v in row[1:]]
    if row_number:
        ws.update([row], f"A{row_number}:{rowcol_to_a1(row_number, len(header))}", value_input_option="RAW")
    else:
        ws.append_row(row, value_input_option="RAW")
    return {"date": date, "hours": {header[i]: row[i] for i in range(1, len(header)) if row[i] != ""},
            "rates_missing": [n for n, _ in resolved if not next((w.get("rate") for w in workers() if w["name"] == n), None)]}


def hours_report(start_date: str = None, end_date: str = None) -> dict:
    """Hours per person (and pay, where a rate is set) for a date range, from the grid."""
    grid = _hours_sheet().get_all_values()
    if not grid:
        return {"people": {}, "total_hours": 0, "total_pay": 0}
    header, rates = grid[0], {w["name"]: w.get("rate") for w in workers()}
    people = {}
    for row in grid[1:]:
        if not row or not row[0] or (start_date and row[0] < start_date) or (end_date and row[0] > end_date):
            continue
        for col, name in enumerate(header[1:], start=1):
            hours = _number(row[col]) if col < len(row) else 0
            if hours:
                p = people.setdefault(name, {"hours": 0.0, "days": 0})
                p["hours"] = round(p["hours"] + hours, 2)
                p["days"] += 1
    for name, p in people.items():
        rate = rates.get(name)
        p["rate"] = rate
        p["pay"] = round(p["hours"] * rate, 2) if rate else None
    return {"period": {"start": start_date, "end": end_date}, "people": people,
            "total_hours": round(sum(p["hours"] for p in people.values()), 2),
            "total_pay": round(sum(p["pay"] or 0 for p in people.values()), 2),
            "missing_rates": [n for n, p in people.items() if p["rate"] is None]}
