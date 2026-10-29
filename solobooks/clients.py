"""
A small client list: name, phone, email, address and notes, plus exact per-client
numbers worked out from the Invoices and Income tabs. Names are matched loosely
("the Hendersons", "Henderson", "Mike Henderson"), and an ambiguous name is never
guessed: the agent is given the candidates and asks the owner.
"""
import datetime
import re
from . import books

STOP_WORDS = {"the", "family", "and", "mr", "mrs", "ms", "dr", "inc", "llc", "co"}


def _tokens(name: str) -> set:
    words = re.findall(r"[a-z0-9']+", (name or "").lower().replace("’", "'"))
    tokens = set()
    for word in words:
        word = word.removesuffix("'s").rstrip("'")
        if word in STOP_WORDS:
            continue
        # "Hendersons" -> "henderson", but keep short words like "bass" or "gus" whole
        tokens.add(word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word)
    return tokens


def matches(query: str, name: str) -> bool:
    """True when every word of the query appears in the name, or every word of the name in the query."""
    q, n = _tokens(query), _tokens(name)
    return bool(q and n) and (q <= n or n <= q)


def find(query: str) -> list:
    query = (query or "").strip()
    found = []
    for client in books.entries("client"):
        haystack = " ".join(str(client.get(f) or "") for f in ("phone", "email")).lower()
        if matches(query, client.get("name")) or (len(query) >= 5 and query.lower() in haystack):
            found.append(client)
    return found


def _clean(fields: dict) -> dict:
    cleaned = {k: (str(v).strip() if v is not None else None) for k, v in fields.items()}
    if cleaned.get("email"):
        cleaned["email"] = cleaned["email"].lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cleaned["email"]):
            raise ValueError(f"That doesn't look like an email address: {fields['email']}")
    return cleaned


def add(name: str, phone=None, email=None, address=None, notes=None, source=None, confirm=False):
    """Adds a client, unless one with a similar name, phone or email already exists (then the agent asks)."""
    fields = _clean({"name": name, "phone": phone, "email": email, "address": address, "notes": notes})
    if not fields["name"]:
        raise ValueError("A client needs a name")
    if not confirm:
        similar = [c for c in books.entries("client")
                   if matches(fields["name"], c.get("name"))
                   or (fields["email"] and fields["email"] == c.get("email"))
                   or (fields["phone"] and _digits(fields["phone"]) and _digits(fields["phone"]) == _digits(c.get("phone")))]
        if similar:
            return {"not_added": True, "possible_existing": similar,
                    "hint": "Ask whether this is one of these clients (then update_client) or a new one (confirm=true)"}
    return books.add("client", {**fields, "source": source})


def _digits(phone) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def update(client_id: str, changes: dict, append_note: str = None):
    client = next((c for c in books.entries("client") if c["id"].upper() == (client_id or "").upper()), None)
    if client is None:
        return None
    changes = _clean({k: v for k, v in (changes or {}).items() if k in ("name", "phone", "email", "address", "notes")})
    if append_note:
        stamp = datetime.date.today().isoformat()
        changes["notes"] = "; ".join(filter(None, [client.get("notes"), f"{append_note.strip()} ({stamp})"]))
    return books.update(client["id"], changes)


def resolve(name: str, source: str = None):
    """
    The client an invoice is for: (client, None) when one matches or a new one was created,
    or (None, candidates) when the name is ambiguous.
    """
    found = find(name)
    if len(found) == 1:
        return found[0], None
    if len(found) > 1:
        exact = [c for c in found if _tokens(c["name"]) == _tokens(name)]
        if len(exact) == 1:
            return exact[0], None
        return None, found
    return books.add("client", {"name": name.strip(), "source": source}), None


def summary(client: dict, today: str) -> dict:
    """Exact numbers for one client from the Invoices and Income tabs."""
    invoices = [i for i in books.entries("invoice") if matches(client["name"], i.get("client"))]
    income = [e for e in books.entries("income") if matches(client["name"], e.get("payer"))]
    paid = [i for i in invoices if i.get("status") == "paid"]
    open_ = [i for i in invoices if i.get("status") != "paid"]

    days_to_pay = []
    for i in paid:
        try:
            days_to_pay.append((datetime.date.fromisoformat(i["paid_on"]) - datetime.date.fromisoformat(i["issued"])).days)
        except (TypeError, ValueError):
            pass
    dates = [d for d in [i.get("issued") for i in invoices] + [e.get("date") for e in income] if d]
    return {
        "client": {k: client.get(k) for k in ("id", "name", "phone", "email", "address", "notes")},
        "invoices": len(invoices),
        "billed": round(sum(i.get("total") or 0 for i in invoices), 2),
        "received": round(sum(e.get("amount") or 0 for e in income), 2),
        "open_invoices": [{"id": i["id"], "total": i["total"], "due": i["due"],
                           "days_overdue": (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(i["due"])).days}
                          for i in open_],
        "owed": round(sum(i.get("total") or 0 for i in open_), 2),
        "average_days_to_pay": round(sum(days_to_pay) / len(days_to_pay), 1) if days_to_pay else None,
        "last_activity": max(dates) if dates else None,
    }
