"""
Invoices: created by text or voice, each with a private web page, a PDF and a
"Pay now" button (Stripe Checkout). When the client pays, the invoice is marked
paid and the income is recorded, exactly once, however many times the payment
is reported (the redirect back from Stripe and the webhook).
"""
import datetime
import json
import secrets
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlsplit
import stripe
from fpdf import FPDF
from . import books, config, memory

AGING_BUCKETS = [(0, "not due yet"), (1, "1-30 days overdue"), (31, "31-60 days overdue"),
                 (61, "61-90 days overdue"), (91, "over 90 days overdue")]


def _cents(value) -> int:
    """Exact money: decimal arithmetic, half a cent rounds up (like a calculator, unlike float round())."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _line_cents(quantity, unit_price) -> int:
    return int((Decimal(str(quantity)) * Decimal(str(unit_price)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _base_url() -> str:
    public = urlsplit(config.PUBLIC_URL)
    return f"{public.scheme}://{public.netloc}" if public.netloc else ""


def items_of(invoice: dict) -> list:
    items = invoice.get("items")
    return json.loads(items) if isinstance(items, str) and items else (items or [])


def _clean_items(items: list) -> list:
    """Validated line items with totals computed in cents, so they always add up exactly."""
    cleaned = []
    for item in items or []:
        description = str(item.get("description") or "").strip()
        quantity = float(item.get("quantity") or 1)
        unit_price = float(item.get("unit_price") or 0)
        if not description or quantity <= 0 or unit_price <= 0:
            raise ValueError(f"Each line item needs a description, a positive quantity and price: {item}")
        cleaned.append({"description": description, "quantity": quantity, "unit_price": _cents(unit_price) / 100,
                        "amount": _line_cents(quantity, unit_price) / 100})
    if not cleaned:
        raise ValueError("An invoice needs at least one line item")
    return cleaned


def _next_number(existing: list) -> str:
    numbers = [int(i["id"].split("-")[1]) for i in existing if (i.get("id") or "").upper().startswith("INV-")
               and i["id"].split("-")[1].isdigit()]
    return f"INV-{max(numbers, default=0) + 1:04d}"


def create(client: str, items: list, today: str, due_date: str = None, due_in_days: int = None,
           notes: str = None) -> dict:
    cleaned = _clean_items(items)
    if not due_date:
        due_date = (datetime.date.fromisoformat(today) + datetime.timedelta(days=int(due_in_days or 14))).isoformat()
    token = secrets.token_urlsafe(16)
    return books.add("invoice", {
        "id": _next_number(books.entries("invoice")), "issued": today, "client": client.strip(),
        "items": json.dumps(cleaned), "total": sum(_cents(i["amount"]) for i in cleaned) / 100,
        "due": due_date, "status": "open", "token": token, "link": f"{_base_url()}/i/{token}", "notes": notes,
    })


def find_by_token(token: str):
    for invoice in books.entries("invoice"):
        if token and invoice.get("token") == token:
            return invoice
    return None


def find(invoice_id: str):
    for invoice in books.entries("invoice"):
        if (invoice.get("id") or "").upper() == (invoice_id or "").upper():
            return invoice
    return None


def mark_paid(invoice_id: str, paid_with: str, date: str, source: str):
    """
    Marks an invoice paid and records the income. Returns (invoice, newly_paid). Safe to call
    more than once for the same payment: only the first call records anything.
    """
    invoice = find(invoice_id)
    if invoice is None:
        return None, False
    if invoice.get("status") == "paid" or not memory.claim(f"paid:{invoice['id']}"):
        return find(invoice_id), False
    try:
        income = books.add("income", {"date": date, "payer": invoice["client"], "amount": invoice["total"],
                                      "paid_with": paid_with, "purpose": f"{invoice['id']}", "source": source})
        invoice = books.update(invoice["id"], {"status": "paid", "paid_on": date, "paid_with": paid_with,
                                               "income_id": income["id"]})
    except Exception:
        memory.unclaim(f"paid:{invoice['id']}")
        raise
    return invoice, True


def days_overdue(invoice: dict, today: str) -> int:
    return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(invoice["due"])).days


def open_invoices(today: str) -> dict:
    """Unpaid invoices with days overdue, an aging summary and the total owed."""
    found = []
    for invoice in books.entries("invoice"):
        if invoice.get("status") != "paid":
            found.append({**{k: invoice.get(k) for k in ("id", "client", "total", "issued", "due", "link",
                                                         "last_reminder")},
                          "days_overdue": days_overdue(invoice, today)})
    found.sort(key=lambda i: -i["days_overdue"])
    aging = {}
    for invoice in found:
        label = [name for start, name in AGING_BUCKETS if invoice["days_overdue"] >= start or start == 0][-1]
        aging[label] = round(aging.get(label, 0) + invoice["total"], 2)
    return {"open_invoices": found, "total_owed": round(sum(i["total"] for i in found), 2), "aging": aging}


# --- PDF -------------------------------------------------------------------------

def _latin1(text) -> str:
    """The built-in PDF fonts are Latin-1 only; replace anything else."""
    return str(text if text is not None else "").encode("latin-1", "replace").decode("latin-1")


def _money(value) -> str:
    return f"${float(value):,.2f}"


def pdf(invoice: dict) -> bytes:
    doc = FPDF()
    doc.add_page()
    doc.set_auto_page_break(True, margin=15)
    doc.set_font("Helvetica", "B", 20)
    doc.cell(0, 10, _latin1(config.BUSINESS_NAME or config.OWNER_NAME), new_x="LMARGIN", new_y="NEXT")
    doc.set_font("Helvetica", "", 10)
    if config.BUSINESS_CONTACT:
        doc.cell(0, 6, _latin1(config.BUSINESS_CONTACT), new_x="LMARGIN", new_y="NEXT")
    doc.ln(6)
    doc.set_font("Helvetica", "B", 14)
    doc.cell(0, 8, _latin1(f"Invoice {invoice['id']}"), new_x="LMARGIN", new_y="NEXT")
    doc.set_font("Helvetica", "", 11)
    for label, value in (("Bill to", invoice["client"]), ("Issued", invoice["issued"]), ("Due", invoice["due"]),
                         ("Status", "PAID" if invoice.get("status") == "paid" else "Open")):
        doc.cell(30, 7, label)
        doc.cell(0, 7, _latin1(value), new_x="LMARGIN", new_y="NEXT")
    doc.ln(4)

    doc.set_font("Helvetica", "B", 11)
    widths = (100, 20, 35, 35)
    for header, width, align in zip(("Description", "Qty", "Unit price", "Amount"), widths, "LRRR"):
        doc.cell(width, 8, header, border="B", align=align)
    doc.ln()
    doc.set_font("Helvetica", "", 11)
    for item in items_of(invoice):
        doc.cell(widths[0], 8, _latin1(item["description"])[:60])
        doc.cell(widths[1], 8, f"{item['quantity']:g}", align="R")
        doc.cell(widths[2], 8, _money(item["unit_price"]), align="R")
        doc.cell(widths[3], 8, _money(item["amount"]), align="R")
        doc.ln()
    doc.set_font("Helvetica", "B", 12)
    doc.cell(sum(widths[:3]), 10, "Total", border="T", align="R")
    doc.cell(widths[3], 10, _money(invoice["total"]), border="T", align="R")
    doc.ln(14)
    doc.set_font("Helvetica", "", 10)
    if invoice.get("notes"):
        doc.multi_cell(0, 6, _latin1(invoice["notes"]), new_x="LMARGIN", new_y="NEXT")
    if invoice.get("status") != "paid" and invoice.get("link"):
        doc.multi_cell(0, 6, _latin1(f"Pay online: {invoice['link']}"))
    return bytes(doc.output())


# --- Stripe ----------------------------------------------------------------------

def checkout_url(invoice: dict) -> str:
    """A Stripe Checkout page for the invoice's line items."""
    stripe.api_key = config.STRIPE_SECRET_KEY
    page = f"{_base_url()}/i/{invoice['token']}"
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"quantity": 1, "price_data": {
            "currency": config.CURRENCY, "unit_amount": _cents(item["amount"]),
            "product_data": {"name": f"{item['description']} ({item['quantity']:g} x {_money(item['unit_price'])})"}}}
            for item in items_of(invoice)],
        client_reference_id=invoice["id"],
        metadata={"invoice_id": invoice["id"]},
        success_url=page + "/paid?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=page,
    )
    return session.url


def paid_invoice_id(session) -> str:
    """The invoice a completed, paid Checkout Session belongs to, or None."""
    if (session.get("payment_status") if isinstance(session, dict) else session.payment_status) != "paid":
        return None
    metadata = session.get("metadata") if isinstance(session, dict) else session.metadata
    return (metadata or {}).get("invoice_id")


def check_session(session_id: str) -> str:
    """Asks Stripe directly whether a Checkout Session was paid (never trusts the browser)."""
    stripe.api_key = config.STRIPE_SECRET_KEY
    return paid_invoice_id(stripe.checkout.Session.retrieve(session_id))
