"""
Texts to clients (invoices, reminders) from the business number. Nothing is sent by the
agent directly: it prepares the exact message, the owner sees it, and the server sends it
only when the owner's very next message is YES (checked in code, not by the model).
"""
import re
from . import clients, config, invoices, memory

YES = {"yes", "y", "yes send", "yes send it", "send", "send it", "ok send", "yep", "yes please"}
NO = {"no", "n", "cancel", "dont send", "don't send", "do not send", "stop", "nope"}


def e164(phone):
    """US phone numbers to +1XXXXXXXXXX (international numbers must already start with +)."""
    if not phone:
        return None
    phone = str(phone).strip()
    digits = re.sub(r"\D", "", phone)
    if phone.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z' ]", "", (text or "").lower()).strip()


def is_yes(text: str) -> bool:
    return _normalize(text) in YES


def is_no(text: str) -> bool:
    return _normalize(text) in NO


def client_for(name: str):
    found = clients.find(name)
    exact = [c for c in found if (c.get("name") or "").lower() == (name or "").lower()]
    return (exact or found or [None])[0] if len(exact) == 1 or len(found) == 1 else None


def prepare(owner: str, invoice_id: str, message: str = None, phone: str = None) -> dict:
    """Builds the text for an invoice (or a reminder about it) and waits for the owner's YES."""
    invoice = invoices.find(invoice_id)
    if invoice is None:
        return {"error": f"No invoice {invoice_id}"}
    client = client_for(invoice["client"])
    to = e164(phone or (client or {}).get("phone"))
    if not to:
        return {"error": f"No usable phone number for {invoice['client']}. Ask the owner for it."}
    if client and phone and not client.get("phone"):
        clients.update(client["id"], {"phone": phone})          # remember it for next time

    business = config.BUSINESS_NAME or config.OWNER_NAME
    first_name = invoice["client"].split()[0].strip(",")
    body = (message or f"Hi {first_name}, this is {business}. Your invoice {invoice['id']} for "
                       f"${invoice['total']:,.2f} is due {invoice['due']}. View and pay here: {invoice['link']}").strip()
    if invoice["link"] not in body:
        body += f" {invoice['link']}"
    if business.lower() not in body.lower():
        body = f"{business}: {body}"
    body += " Reply STOP to opt out."

    memory.set_pending(owner, {"to": to, "body": body, "invoice_id": invoice["id"], "client": invoice["client"]})
    return {"not_sent_yet": True, "to": to, "client": invoice["client"], "message": body,
            "next": "Show the owner this exact message and the number, and ask them to reply YES to send or NO to cancel."}
