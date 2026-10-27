"""
The SoloBooks agent: Muse Spark with tools over the books.

The model decides what to record, change, delete or look up; the tools do the
actual work and all math (totals, deductions) happens in code, so every number
the agent quotes comes from a tool result.
"""
import datetime
import json
from zoneinfo import ZoneInfo
from openai import OpenAI
from . import books, config
from .trades import get_trade

client = OpenAI(base_url=config.META_BASE_URL, api_key=config.MODEL_API_KEY or "not-set")

MAX_STEPS = 8


def today() -> str:
    return datetime.datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()


def _money(value):
    return round(value or 0, 2)


# --- Tools -------------------------------------------------------------------

def record_expense(date, vendor, total, category, tax=None, paid_with=None, notes=None, _ctx=None):
    return books.add("expense", dict(date=date, vendor=vendor, total=total, tax=tax, category=category,
                                     paid_with=paid_with, notes=notes, photo=_ctx.get("photo"),
                                     source=_ctx.get("source")))


def record_income(date, payer, amount, paid_with=None, purpose=None, _ctx=None):
    return books.add("income", dict(date=date, payer=payer, amount=amount, paid_with=paid_with,
                                    purpose=purpose, source=_ctx.get("source")))


def record_trip(date, miles, destination, purpose=None, _ctx=None):
    return books.add("trip", dict(date=date, miles=miles, destination=destination, purpose=purpose,
                                  source=_ctx.get("source")))


def update_entry(entry_id, changes, _ctx=None):
    entry = books.update(entry_id, changes)
    return entry or {"error": f"No entry with ID {entry_id}"}


def delete_entry(entry_id, _ctx=None):
    entry = books.delete(entry_id)
    return {"deleted": entry} if entry else {"error": f"No entry with ID {entry_id}"}


def _in_range(entry, start_date, end_date):
    date = entry.get("date") or ""
    return (not start_date or date >= start_date) and (not end_date or date <= end_date)


def find_entries(kind=None, start_date=None, end_date=None, text=None, _ctx=None):
    kinds = [kind] if kind in books.TABS else list(books.TABS)
    found = []
    for k in kinds:
        for entry in books.entries(k):
            haystack = " ".join(str(v) for v in entry.values() if v is not None).lower()
            if _in_range(entry, start_date, end_date) and (not text or text.lower() in haystack):
                found.append(entry)
    found.sort(key=lambda e: e.get("date") or "")
    return {"count": len(found), "entries": found[-25:]}


def get_totals(start_date=None, end_date=None, _ctx=None):
    expenses = [e for e in books.entries("expense") if _in_range(e, start_date, end_date)]
    income = [e for e in books.entries("income") if _in_range(e, start_date, end_date)]
    trips = [e for e in books.entries("trip") if _in_range(e, start_date, end_date)]

    by_category = {}
    for e in expenses:
        by_category[e.get("category") or "Other"] = _money(by_category.get(e.get("category") or "Other", 0) + (e.get("total") or 0))

    total_income = _money(sum(e.get("amount") or 0 for e in income))
    total_expenses = _money(sum(e.get("total") or 0 for e in expenses))
    deduction = _money(sum(e.get("deduction") or 0 for e in trips))
    return {
        "period": {"start": start_date, "end": end_date},
        "income": total_income, "income_entries": len(income),
        "expenses": total_expenses, "expense_entries": len(expenses),
        "expenses_by_category": by_category,
        "profit": _money(total_income - total_expenses),
        "business_miles": round(sum(e.get("miles") or 0 for e in trips), 1),
        "mileage_deduction": deduction,
        "profit_after_mileage_deduction": _money(total_income - total_expenses - deduction),
    }


TOOLS = {f.__name__: f for f in (record_expense, record_income, record_trip, update_entry,
                                  delete_entry, find_entries, get_totals)}


def _schema(categories):
    date = {"type": "string", "description": "YYYY-MM-DD"}
    text = {"type": "string"}
    number = {"type": "number"}

    def tool(name, description, properties, required):
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}

    return [
        tool("record_expense", "Record a business expense (a purchase or bill the owner paid).",
             {"date": date, "vendor": text, "total": {**number, "description": "Total paid, including tax"},
              "tax": number, "category": {"type": "string", "enum": categories},
              "paid_with": {**text, "description": "e.g. cash, Visa ****1234"},
              "notes": {**text, "description": "What was bought / which job"}},
             ["date", "vendor", "total", "category"]),
        tool("record_income", "Record money the owner received from a customer.",
             {"date": date, "payer": text, "amount": number,
              "paid_with": {**text, "description": "e.g. cash, Venmo, Zelle, check"},
              "purpose": {**text, "description": "Which job or service"}},
             ["date", "payer", "amount"]),
        tool("record_trip", "Record business driving for the mileage tax deduction.",
             {"date": date, "miles": {**number, "description": "Total miles, both ways for a round trip"},
              "destination": text, "purpose": {**text, "description": "Business reason for the trip"}},
             ["date", "miles", "destination"]),
        tool("update_entry", "Change fields of an existing entry by its ID.",
             {"entry_id": text, "changes": {"type": "object", "description":
                 "Fields to change, e.g. {\"total\": 45.2} or {\"category\": \"Fuel\"}"}},
             ["entry_id", "changes"]),
        tool("delete_entry", "Delete an entry by its ID.", {"entry_id": text}, ["entry_id"]),
        tool("find_entries", "Look up recorded entries, optionally by kind, date range and text.",
             {"kind": {"type": "string", "enum": ["expense", "income", "trip"]},
              "start_date": date, "end_date": date, "text": text}, []),
        tool("get_totals", "Exact totals for a date range: income, expenses by category, profit, "
                           "business miles and mileage deduction.",
             {"start_date": date, "end_date": date}, []),
    ]


def _system_prompt(trade, recent):
    lines = "\n".join(
        f"- {e['id']} {e['kind']} {e.get('date')} "
        + {"expense": f"{e.get('vendor')} ${e.get('total')} [{e.get('category')}]",
           "income": f"from {e.get('payer')} ${e.get('amount')}",
           "trip": f"{e.get('miles')} mi to {e.get('destination')}"}[e["kind"]]
        for e in recent) or "- (nothing recorded yet)"
    return f"""You are SoloBooks, the back-office assistant of {config.OWNER_NAME}, a solo {trade['label']}.
They text you while working; you keep their books in a Google Sheet (expenses, income, business mileage).
Today is {today()}.

How to work:
- Record things only by calling tools. Never say something is saved, changed or deleted unless a tool result shows it.
- One message can mention several things (a trip, a purchase and a payment): record each one separately.
- If the amount (or miles) is missing, ask instead of guessing. If a trip has no business purpose, record it and ask what it was for.
- "Fix it", "delete that" etc. refer to the most recent entries below; use their IDs. Use find_entries to locate older ones.
- For any question about money or miles, call get_totals or find_entries and quote the exact numbers from the result.
- Expense categories: {", ".join(trade['categories'])}.

Your reply is a text message: plain text, no markdown, short (max 8 lines).
Confirm what you did with the entry IDs, e.g. "✅ Recorded fuel $64.10 at Shell (E3F9A)".

Most recent entries (newest first):
{lines}"""


def run(text: str, images: list = None, history: list = None, source: str = "text", photo: str = None) -> str:
    """
    Handles one message from the owner. images are data URLs; history is a list
    of earlier {"role", "content"} text turns. Returns the reply to text back.
    """
    trade = get_trade(config.TRADE)
    content = [{"type": "text", "text": text or "(The owner sent this without a message.)"}]
    for image in images or []:
        content.append({"type": "image_url", "image_url": {"url": image}})

    messages = [{"role": "system", "content": _system_prompt(trade, books.recent())}]
    messages += history or []
    messages.append({"role": "user", "content": content})
    ctx = {"source": source, "photo": photo}
    schema = _schema(trade["categories"])

    for _ in range(MAX_STEPS):
        response = client.chat.completions.create(model=config.MUSE_MODEL, messages=messages, tools=schema)
        message = response.choices[0].message
        if not message.tool_calls:
            return (message.content or "").strip() or "Done."

        messages.append({"role": "assistant", "content": message.content or "",
                         "tool_calls": [call.model_dump() for call in message.tool_calls]})
        for call in message.tool_calls:
            result = _call_tool(call.function.name, call.function.arguments, ctx)
            print(f"🔧 {call.function.name}({call.function.arguments}) -> {json.dumps(result)[:300]}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})

    return "Sorry, that took too many steps. Please try again with a shorter message."


def _call_tool(name, arguments, ctx):
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"Unknown tool {name}"}
    try:
        args = json.loads(arguments or "{}")
        return tool(**args, _ctx=ctx)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
