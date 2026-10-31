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
from . import books, checks, clients, config, crew, invoices, outbox
from .trades import get_trade

client = OpenAI(base_url=config.META_BASE_URL, api_key=config.MODEL_API_KEY or "not-set")

MAX_STEPS = 8


def today() -> str:
    return datetime.datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()


def _money(value):
    return round(value or 0, 2)


# --- Tools -------------------------------------------------------------------

def _photo_for(receipt_number, ctx):
    photos = ctx.get("photos") or []
    if receipt_number and 1 <= int(receipt_number) <= len(photos):
        return photos[int(receipt_number) - 1]
    return photos[0] if len(photos) == 1 else None


def _record(kind, fields, confirm, ctx):
    """Records an entry, unless a check finds something to double-check with the owner first."""
    if not confirm:
        issues = checks.problems(kind, fields, books.entries(kind), today())
        if issues:
            return {"not_recorded": True, "needs_confirmation": issues, "details": fields}
    return books.add(kind, fields)


def record_expense(date, total, category, vendor=None, tax=None, paid_with=None, notes=None, receipt_number=None,
                   confirm=False, _ctx=None):
    return _record("expense", dict(date=date, vendor=vendor, total=total, tax=tax, category=category,
                                   paid_with=paid_with, notes=notes, photo=_photo_for(receipt_number, _ctx),
                                   source=_ctx.get("source")), confirm, _ctx)


def record_income(date, payer, amount, paid_with=None, purpose=None, confirm=False, _ctx=None):
    return _record("income", dict(date=date, payer=payer, amount=amount, paid_with=paid_with,
                                  purpose=purpose, source=_ctx.get("source")), confirm, _ctx)


def record_trip(date, miles, destination, purpose=None, confirm=False, _ctx=None):
    return _record("trip", dict(date=date, miles=miles, destination=destination, purpose=purpose,
                                source=_ctx.get("source")), confirm, _ctx)


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
        # Entries that look recorded twice: [first id, second id, description]
        "possible_duplicates": checks.duplicates_in(expenses + income + trips),
    }


def create_invoice(client, items, due_date=None, due_in_days=None, notes=None, _ctx=None):
    record, candidates = clients.resolve(client, source=_ctx.get("source"))
    if candidates:
        return {"not_created": True, "ambiguous_client": [{"id": c["id"], "name": c["name"], "address": c.get("address")}
                                                          for c in candidates],
                "hint": "Ask the owner which client, then call again with that client's exact name"}
    invoice = invoices.create(record["name"], items, today(), due_date=due_date, due_in_days=due_in_days, notes=notes)
    return {k: invoice.get(k) for k in ("id", "client", "total", "issued", "due", "link")} | {
        "items": invoices.items_of(invoice), "client_id": record["id"], "client_email": record.get("email")}


def add_client(name, phone=None, email=None, address=None, notes=None, confirm=False, _ctx=None):
    return clients.add(name, phone=phone, email=email, address=address, notes=notes,
                       source=_ctx.get("source"), confirm=confirm)


def update_client(client_id, changes=None, append_note=None, _ctx=None):
    return clients.update(client_id, changes or {}, append_note=append_note) or {"error": f"No client {client_id}"}


def find_clients(query, _ctx=None):
    found = clients.find(query)
    return {"count": len(found), "clients": [clients.summary(c, today()) for c in found[:5]]}


def list_invoices(_ctx=None):
    return invoices.open_invoices(today())


def mark_invoice_paid(invoice_id, paid_with, date=None, _ctx=None):
    invoice, newly_paid = invoices.mark_paid(invoice_id, paid_with, date or today(), source=_ctx.get("source"))
    if invoice is None:
        return {"error": f"No invoice {invoice_id}"}
    if not newly_paid:
        return {"already_paid": True, "invoice": invoice["id"], "paid_on": invoice.get("paid_on")}
    return {"paid": invoice["id"], "client": invoice["client"], "amount": invoice["total"],
            "income_entry": invoice["income_id"]}


def log_hours(date, workers, mode="add", confirm_new=False, _ctx=None):
    return crew.log_hours(date, workers, mode=mode, confirm_new=confirm_new)


def crew_hours(start_date=None, end_date=None, _ctx=None):
    return crew.hours_report(start_date, end_date)


def add_worker(name, rate=None, phone=None, notes=None, confirm=False, _ctx=None):
    return crew.add_worker(name, rate=rate, phone=phone, notes=notes, confirm=confirm)


def text_client(invoice_id, message=None, phone=None, _ctx=None):
    return outbox.prepare(config.OWNER_PHONE_NUMBER, invoice_id, message=message, phone=phone)


TOOLS = {f.__name__: f for f in (text_client, log_hours, crew_hours, add_worker, record_expense, record_income, record_trip, update_entry,
                                  delete_entry, find_entries, get_totals, create_invoice, list_invoices,
                                  mark_invoice_paid, add_client, update_client, find_clients)}


def _schema(categories):
    date = {"type": "string", "description": "YYYY-MM-DD"}
    text = {"type": "string"}
    number = {"type": "number"}
    confirm = {"type": "boolean", "description":
               "Only true after the owner confirmed an entry that a previous call flagged in needs_confirmation"}

    def tool(name, description, properties, required):
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}

    return [
        tool("record_expense", "Record a business expense (a purchase or bill the owner paid).",
             {"date": date, "vendor": text, "total": {**number, "description": "Total paid, including tax"},
              "tax": number, "category": {"type": "string", "enum": categories},
              "paid_with": {**text, "description": "e.g. cash, Visa ****1234"},
              "notes": {**text, "description": "What was bought / which job"},
              "receipt_number": {"type": "integer", "description":
                  "When several receipt images were sent: which image (1, 2, 3...) this expense comes from"},
              "confirm": confirm},
             ["date", "total", "category"]),
        tool("record_income", "Record money the owner received from a customer.",
             {"date": date, "payer": text, "amount": number,
              "paid_with": {**text, "description": "e.g. cash, Venmo, Zelle, check"},
              "purpose": {**text, "description": "Which job or service"}, "confirm": confirm},
             ["date", "payer", "amount"]),
        tool("record_trip", "Record business driving for the mileage tax deduction.",
             {"date": date, "miles": {**number, "description": "Total miles, both ways for a round trip"},
              "destination": text, "purpose": {**text, "description": "Business reason for the trip"},
              "confirm": confirm},
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
        tool("create_invoice", "Create an invoice for a client. Totals are computed by the tool. Returns the "
                               "invoice number and a private link (web page with Pay now and Download PDF).",
             {"client": text,
              "items": {"type": "array", "description": "Line items", "items": {"type": "object", "properties": {
                  "description": text, "quantity": {**number, "description": "Hours, units... (1 for a flat fee)"},
                  "unit_price": {**number, "description": "Price per unit (or the flat fee)"}},
                  "required": ["description", "unit_price"]}},
              "due_date": date, "due_in_days": {"type": "integer", "description": "Used if no due_date; default 14"},
              "notes": {**text, "description": "Optional note shown on the invoice"}},
             ["client", "items"]),
        tool("list_invoices", "Unpaid invoices with days overdue, total owed and an aging summary.", {}, []),
        tool("mark_invoice_paid", "Mark an open invoice paid (cash, check, Zelle...). Also records the income.",
             {"invoice_id": text, "paid_with": text, "date": date}, ["invoice_id", "paid_with"]),
        tool("add_client", "Add a client to the client list (from a text, voice note or a photo of a business card).",
             {"name": text, "phone": text, "email": text, "address": text, "notes": text,
              "confirm": {"type": "boolean", "description": "Only true after the owner confirmed it is a new client "
                                                            "despite possible_existing"}},
             ["name"]),
        tool("update_client", "Change a client's details, or add a note (gate codes, preferences...).",
             {"client_id": text, "changes": {"type": "object", "description":
                 "Fields to change: name, phone, email, address, notes"},
              "append_note": {**text, "description": "A note to add to the client's existing notes"}},
             ["client_id"]),
        tool("text_client", "Prepare a text message to a client about an invoice (sending it, or a payment reminder). "
                            "It is NOT sent: the owner must reply YES to your next message.",
             {"invoice_id": text, "message": {**text, "description": "Optional custom text, e.g. a polite reminder. "
                                                                      "The invoice link is added if missing."},
              "phone": {**text, "description": "Only if the client has no phone on file and the owner gave one"}},
             ["invoice_id"]),
        tool("log_hours", "Record hours worked by crew members on one date (the Crew Hours grid).",
             {"date": date, "workers": {"type": "array", "items": {"type": "object", "properties": {
                 "name": text, "hours": number}, "required": ["name", "hours"]}},
              "mode": {"type": "string", "enum": ["add", "set"], "description":
                  "add: add to hours already logged that day (default); set: correct the day's hours"},
              "confirm_new": {"type": "boolean", "description": "Only true after the owner confirmed a new crew member"}},
             ["date", "workers"]),
        tool("crew_hours", "Exact hours, days and pay per crew member for a date range.",
             {"start_date": date, "end_date": date}, []),
        tool("add_worker", "Add a crew member with their hourly rate.",
             {"name": text, "rate": {**number, "description": "Dollars per hour"}, "phone": text, "notes": text,
              "confirm": {"type": "boolean"}}, ["name"]),
        tool("find_clients", "Look up clients by name, phone or email, with their contact details and exact numbers: "
                             "invoices, billed, received, owed, average days to pay.", {"query": text}, ["query"]),
    ]


def _system_prompt(trade, recent):
    lines = "\n".join(
        f"- {e['id']} {e['kind']} {e.get('date')} "
        + {"expense": f"{e.get('vendor') or '(no vendor)'} ${e.get('total')} [{e.get('category')}]",
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
  If an expense doesn't say where it was bought, record it without a vendor (never write "Unknown") and ask where;
  when they answer, set the vendor with update_entry.
- "Fix it", "delete that" etc. refer to the most recent entries below; use their IDs. Use find_entries to locate older ones.
- For any question about money or miles, call get_totals or find_entries and quote the exact numbers from the result.
  If get_totals lists possible_duplicates, point them out with their IDs and offer to delete the extra one.
- If a record tool returns needs_confirmation, nothing was saved: ask the owner one short question that restates
  the details and the concern (e.g. "Looks like E3F9A, Shell $64.10 on Oct 12. Record it again?"). Only after
  they confirm, call the tool again with the same details and confirm=true. Other entries from the same message
  are recorded normally.
- Receipt photos: read the date, vendor, total, tax and payment method. When several receipt images are sent,
  record each one as its own expense with its receipt_number. If one is unreadable, say which number.
- Expense categories: {", ".join(trade['categories'])}.
- Invoices: to bill a client, call create_invoice with line items (description, quantity, unit_price). Reply with the
  invoice number, total, due date and the link, plus one short line the owner can forward to the client with it.
  For "who owes me" call list_invoices. When a client pays and has an open invoice for that amount (check
  list_invoices), call mark_invoice_paid instead of record_income; it records the income too. When asked for a
  reminder, write a short, polite message in the owner's voice with the invoice number, amount, due date and link
  (firmer if over 30 days overdue) for the owner to forward.
- Texting clients: to send an invoice or a reminder to a client, call text_client. It only prepares the message.
  Reply with the exact message and number, ending with "Reply YES to send or NO to cancel." Never say it was sent;
  the server sends it only when the owner replies YES.
- Crew: "Diego and Jonny worked 4 hours today" -> log_hours with each person. If log_hours returns problems
  (a name not on the crew list, ambiguous, impossible hours), ask the owner; never invent a new crew member.
  For "what do I owe Diego this week" use crew_hours (pay = hours x rate, computed by the tool; if a rate is
  missing, ask for it and set it with update_entry on the W... ID). Paying the crew is an expense in "Crew wages".
- Clients: use add_client for new customers, update_client for changes and notes ("gate code 4471"), and
  find_clients for questions about a customer ("what's the Hendersons' address?", "tell me about Lee"). A photo of a
  business card or contact details is a new client, not a receipt. If add_client returns possible_existing or
  create_invoice returns ambiguous_client, ask the owner which one; never guess.

Your reply is a text message: plain text, no markdown, short (max 8 lines).
Confirm what you did with the entry IDs, e.g. "✅ Recorded fuel $64.10 at Shell (E3F9A)".

Most recent entries (newest first):
{lines}"""


def run(text: str, images: list = None, history: list = None, source: str = "text", photos: list = None) -> str:
    """
    Handles one message from the owner. images are data URLs (photos holds the
    saved link for each); history is a list of earlier {"role", "content"} text
    turns. Returns the reply to text back.
    """
    trade = get_trade(config.TRADE)
    content = [{"type": "text", "text": text or "(The owner sent this without a message.)"}]
    for image in images or []:
        content.append({"type": "image_url", "image_url": {"url": image}})

    messages = [{"role": "system", "content": _system_prompt(trade, books.recent())}]
    messages += history or []
    messages.append({"role": "user", "content": content})
    ctx = {"source": source, "photos": photos or []}
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
