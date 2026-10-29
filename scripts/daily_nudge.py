"""
Daily check for overdue invoices: texts the owner a short summary with a reminder
for each client, written by Muse Spark, ready to forward. Run once a day, e.g. by cron:
  python -m scripts.daily_nudge
"""
import datetime
from solobooks import agent, books, config, invoices, server

REMIND_EVERY_DAYS = 3


def due_for_reminder(invoice: dict, today: str) -> bool:
    if invoice["days_overdue"] <= 0:
        return False
    last = invoice.get("last_reminder")
    return not last or (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(last)).days >= REMIND_EVERY_DAYS


def draft_reminder(invoice: dict) -> str:
    """A short reminder in the owner's voice; a plain template if the model is unavailable."""
    fallback = (f"Hi {invoice['client']}, a friendly reminder that invoice {invoice['id']} for "
                f"${invoice['total']:,.2f} was due {invoice['due']}. You can pay here: {invoice['link']} Thank you!")
    tone = "polite and friendly" if invoice["days_overdue"] <= 30 else "polite but firm"
    try:
        response = agent.client.chat.completions.create(model=config.MUSE_MODEL, messages=[{"role": "user", "content": (
            f"Write a {tone} payment reminder text message from {config.OWNER_NAME} "
            f"({config.BUSINESS_NAME or 'a solo business'}) to the client {invoice['client']}. Invoice {invoice['id']}, "
            f"${invoice['total']:,.2f}, due {invoice['due']} ({invoice['days_overdue']} days ago). Include this payment "
            f"link exactly: {invoice['link']} . At most 3 sentences, plain text, no placeholders, reply with the message only.")}])
        text = (response.choices[0].message.content or "").strip().strip('"')
        return text if invoice["link"] in text else fallback
    except Exception as e:
        print("Draft error, using the template:", repr(e))
        return fallback


def main():
    today = agent.today()
    summary = invoices.open_invoices(today)
    due = [i for i in summary["open_invoices"] if due_for_reminder(i, today)]
    if not due:
        print(f"{today}: nothing to remind")
        return

    overdue_total = sum(i["total"] for i in summary["open_invoices"] if i["days_overdue"] > 0)
    parts = [f"⏰ {len(due)} invoice{'s' if len(due) > 1 else ''} to follow up (${overdue_total:,.2f} overdue):"]
    for invoice in due:
        parts.append(f"\n{invoice['id']} {invoice['client']} ${invoice['total']:,.2f}, {invoice['days_overdue']} "
                     f"days overdue. Reminder to forward:\n{draft_reminder(invoice)}")
    server.send_text(config.OWNER_PHONE_NUMBER, "\n".join(parts))
    for invoice in due:
        books.update(invoice["id"], {"last_reminder": today})
    print(f"{today}: reminded about {', '.join(i['id'] for i in due)}")


if __name__ == "__main__":
    main()
