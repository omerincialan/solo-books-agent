"""
The books: a Google Sheet with one tab per kind of entry. Every row has a short
ID (E…, I…, T…) so the agent can point at exact entries when it fixes, deletes
or explains something.
"""
import datetime
import secrets
import gspread
from . import config

# kind -> (tab name, [(field, column header), ...])
TABS = {
    "expense": ("Expenses", [
        ("id", "ID"), ("recorded_at", "Recorded at"), ("date", "Date"), ("vendor", "Vendor"),
        ("total", "Total"), ("tax", "Tax"), ("category", "Category"), ("paid_with", "Paid with"),
        ("notes", "Notes"), ("photo", "Receipt photo"), ("source", "Source"),
    ]),
    "income": ("Income", [
        ("id", "ID"), ("recorded_at", "Recorded at"), ("date", "Date"), ("payer", "From"),
        ("amount", "Amount"), ("paid_with", "Paid with"), ("purpose", "For"), ("source", "Source"),
    ]),
    "trip": ("Mileage", [
        ("id", "ID"), ("recorded_at", "Recorded at"), ("date", "Date"), ("miles", "Miles"),
        ("destination", "To"), ("purpose", "Purpose"), ("rate", "Rate per mile"),
        ("deduction", "Deduction"), ("source", "Source"),
    ]),
    "invoice": ("Invoices", [
        ("id", "Invoice"), ("recorded_at", "Recorded at"), ("issued", "Issued"), ("client", "Client"),
        ("items", "Items"), ("total", "Total"), ("due", "Due"), ("status", "Status"),
        ("paid_on", "Paid on"), ("paid_with", "Paid with"), ("income_id", "Income entry"),
        ("link", "Invoice link"), ("token", "Token"), ("last_reminder", "Last reminder"), ("notes", "Notes"),
    ]),
}
PREFIX = {"invoice": "INV-", "expense": "E", "income": "I", "trip": "T"}  # longest first: INV- before I
NUMBER_FIELDS = {"total", "tax", "amount", "miles", "rate", "deduction"}

_sheet = None
_tabs = {}


def _worksheet(kind: str):
    """Opens (or creates, with a header row) the tab for a kind of entry."""
    global _sheet
    if kind not in _tabs:
        if _sheet is None:
            client = gspread.service_account(filename=config.GOOGLE_SERVICE_ACCOUNT_FILE)
            _sheet = client.open_by_key(config.GOOGLE_SHEET_ID)
        name, columns = TABS[kind]
        try:
            _tabs[kind] = _sheet.worksheet(name)
        except gspread.WorksheetNotFound:
            _tabs[kind] = _sheet.add_worksheet(name, rows=1000, cols=len(columns))
            _tabs[kind].append_row([header for _, header in columns])
    return _tabs[kind]


def _fields(kind: str) -> list:
    return [field for field, _ in TABS[kind][1]]


def kind_of(entry_id: str):
    for kind, prefix in PREFIX.items():
        if entry_id and entry_id.upper().startswith(prefix):
            return kind
    return None


def _to_number(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _to_row(kind: str, entry: dict) -> list:
    return ["" if entry.get(f) is None else entry[f] for f in _fields(kind)]


def _from_row(kind: str, row: list) -> dict:
    row = list(row) + [""] * len(_fields(kind))
    entry = {"kind": kind}
    for field, value in zip(_fields(kind), row):
        entry[field] = _to_number(value) if field in NUMBER_FIELDS else (value or None)
    return entry


def _with_deduction(entry: dict) -> dict:
    if entry["kind"] == "trip" and entry.get("miles") is not None:
        entry["rate"] = entry.get("rate") or config.MILEAGE_RATE
        entry["deduction"] = round(entry["miles"] * entry["rate"], 2)
    return entry


def add(kind: str, fields: dict) -> dict:
    entry = {f: fields.get(f) for f in _fields(kind)}
    entry.update(kind=kind,
                 id=fields.get("id") or PREFIX[kind] + secrets.token_hex(2).upper(),
                 recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
    _with_deduction(entry)
    # RAW so text from a receipt or message is never run as a spreadsheet formula
    _worksheet(kind).append_row(_to_row(kind, entry), value_input_option="RAW")
    return entry


def entries(kind: str) -> list:
    rows = _worksheet(kind).get_all_values()[1:]
    return [_from_row(kind, row) for row in rows if row and row[0]]


def _find_row(entry_id: str):
    kind = kind_of(entry_id)
    if kind is None:
        return None, None, None
    ws = _worksheet(kind)
    for number, row in enumerate(ws.get_all_values(), start=1):
        if number > 1 and row and row[0].upper() == entry_id.upper():
            return kind, number, _from_row(kind, row)
    return kind, None, None


def update(entry_id: str, changes: dict):
    kind, row_number, entry = _find_row(entry_id)
    if entry is None:
        return None
    for field, value in changes.items():
        if field in _fields(kind) and field not in ("id", "recorded_at") and value is not None:
            entry[field] = value
    _with_deduction(entry)
    last_column = chr(ord("A") + len(_fields(kind)) - 1)
    _worksheet(kind).update([_to_row(kind, entry)], f"A{row_number}:{last_column}{row_number}", value_input_option="RAW")
    return entry


def delete(entry_id: str):
    kind, row_number, entry = _find_row(entry_id)
    if entry is None:
        return None
    _worksheet(kind).delete_rows(row_number)
    return entry


def recent(limit: int = 8) -> list:
    """The most recently recorded entries of all kinds, newest first."""
    everything = entries("expense") + entries("income") + entries("trip")
    return sorted(everything, key=lambda e: e.get("recorded_at") or "", reverse=True)[:limit]
