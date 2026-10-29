"""
Checks the agent runs before recording, so mistakes are caught while the owner
still remembers: likely duplicates and numbers that don't look right. A failed
check doesn't record anything; the agent asks the owner and records only once
they confirm.
"""
import datetime

DUPLICATE_WINDOW_DAYS = 3
LIMITS = {"expense": 3000, "income": 15000, "trip": 400}  # amounts above these get a double-check


def _days_apart(a, b):
    try:
        return abs((datetime.date.fromisoformat(a) - datetime.date.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return None


def _same_text(a, b):
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    return bool(a and b) and (a in b or b in a)


def _close(a, b):
    return a is not None and b is not None and abs(float(a) - float(b)) < 0.01


def is_duplicate(kind: str, new: dict, old: dict) -> bool:
    days = _days_apart(new.get("date"), old.get("date"))
    if days is None:
        return False
    if kind == "expense":
        return _close(new.get("total"), old.get("total")) and (
            days == 0 or (days <= DUPLICATE_WINDOW_DAYS and _same_text(new.get("vendor"), old.get("vendor"))))
    if kind == "income":
        return days == 0 and _close(new.get("amount"), old.get("amount")) and _same_text(new.get("payer"), old.get("payer"))
    if kind == "trip":
        return days == 0 and _close(new.get("miles"), old.get("miles")) and _same_text(new.get("destination"), old.get("destination"))
    return False


def problems(kind: str, new: dict, existing: list, today: str) -> list:
    """Reasons to double-check with the owner before recording (empty if it looks fine)."""
    found = []
    amount = {"expense": new.get("total"), "income": new.get("amount"), "trip": new.get("miles")}[kind]

    if amount is not None and float(amount) <= 0:
        found.append(f"the {'miles' if kind == 'trip' else 'amount'} is {amount}, which isn't positive")
    elif amount is not None and float(amount) > LIMITS[kind]:
        unit = "miles" if kind == "trip" else "dollars"
        found.append(f"{amount:g} {unit} is unusually high; check it isn't a typo (e.g. a missing decimal point)")

    if kind == "expense" and new.get("tax") is not None and new.get("total"):
        if float(new["tax"]) >= float(new["total"]):
            found.append(f"the tax ({new['tax']}) is not less than the total ({new['total']})")
        elif float(new["tax"]) > 0.2 * float(new["total"]):
            found.append(f"the tax ({new['tax']}) is more than 20% of the total ({new['total']})")

    days_ahead = _days_apart(new.get("date"), today)
    if days_ahead is not None and (new.get("date") or "") > today and days_ahead > 1:
        found.append(f"the date {new['date']} is in the future")
    elif days_ahead is not None and days_ahead > 366:
        found.append(f"the date {new['date']} is more than a year ago")

    for old in existing:
        if is_duplicate(kind, new, old):
            found.append(f"it looks like {old['id']}, already recorded on {old.get('date')}: "
                         + describe(old))
            break
    return found


def describe(entry: dict) -> str:
    return {"expense": lambda e: f"{e.get('vendor') or '(no vendor)'} ${e.get('total')} ({e.get('category')})",
            "income": lambda e: f"${e.get('amount')} from {e.get('payer')}",
            "trip": lambda e: f"{e.get('miles')} mi to {e.get('destination')}"}[entry["kind"]](entry)


def duplicates_in(entries: list) -> list:
    """Pairs of already-recorded entries that look like the same thing, as [id, id, description]."""
    pairs = []
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if a["kind"] == b["kind"] and is_duplicate(a["kind"], b, a):
                pairs.append([a["id"], b["id"], describe(a)])
    return pairs
