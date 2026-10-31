"""
Twilio SMS/MMS webhook. Twilio only waits ~15 seconds for an answer, so the
webhook returns right away and the agent's reply is sent as a new text once
the work is done.
"""
import threading
import requests
import stripe
from flask import Flask, Response, redirect, render_template_string, request, send_from_directory, abort
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from . import agent, books, clients, config, invoices, memory, outbox, photos, shoebox, voice
from .trades import get_trade

app = Flask(__name__)


def is_from_twilio() -> bool:
    if not config.TWILIO_AUTH_TOKEN:
        return False
    url = config.PUBLIC_URL or request.url
    return RequestValidator(config.TWILIO_AUTH_TOKEN).validate(
        url, request.form.to_dict(), request.headers.get("X-Twilio-Signature", ""))


def send_text(to: str, body: str, media_url: str = None):
    Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN).messages.create(
        from_=config.TWILIO_PHONE_NUMBER, to=to, body=body, **({"media_url": [media_url]} if media_url else {}))


def download(url: str) -> bytes:
    r = requests.get(url, auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN), timeout=20)
    r.raise_for_status()
    return r.content


def read_photo(data: bytes, content_type: str):
    """
    Returns (images, links, note, annotated link). A photo with several receipts
    is cut into one image per receipt with SAM 3.1; otherwise the photo is used as is.
    """
    try:
        jpeg = shoebox.prepare(data)
    except Exception as e:
        print("Could not open the photo, sending it as is:", repr(e))
        return [photos.data_url(data, content_type)], [photos.save(data, content_type)], "", None
    try:
        found = shoebox.find_receipts(jpeg)
    except Exception as e:
        print("SAM error, using the whole photo:", repr(e))
        found = []
    if len(found) < 2:
        return [photos.data_url(jpeg, "image/jpeg")], [photos.save(jpeg, "image/jpeg")], "", None

    crops = shoebox.cut_out(jpeg, [f.box for f in found])
    links = [photos.save(crop, "image/jpeg") for crop in crops]
    annotated = photos.save(shoebox.annotate(jpeg, found), "image/jpeg")
    note = (f"\n(This photo contains {len(crops)} receipts. They were cut out with SAM and sent as images "
            f"1 to {len(crops)}: record each one with its receipt_number.)")
    return [photos.data_url(c, "image/jpeg") for c in crops], links, note, annotated


def answer_pending(number: str, text: str) -> bool:
    """
    If a client text is waiting for the owner's confirmation, this message decides it: YES sends it,
    NO cancels it, anything else cancels it and is handled normally. Returns True when handled here.
    """
    if not memory.has_pending(number):
        return False
    pending = memory.take_pending(number)
    if outbox.is_yes(text):
        if pending is None:
            send_text(number, "⌛ That send request expired. Ask me again and I'll prepare it.")
        else:
            send_text(pending["to"], pending["body"])
            reply = f"📤 Sent {pending['invoice_id']} to {pending['client']} ({pending['to']})."
            memory.add(number, "user", text)
            memory.add(number, "assistant", reply)
            send_text(number, reply)
        return True
    if outbox.is_no(text):
        send_text(number, "OK, not sent.")
        return True
    return False


def forward_from_client(number: str, text: str, media: list):
    """Messages from anyone but the owner (clients replying to an invoice) go to the owner."""
    digits = clients._digits(number)
    try:
        client = next((c for c in books.entries("client") if digits and clients._digits(c.get("phone")) == digits), None)
    except Exception as e:                       # the message still reaches the owner without the name
        print("Client lookup failed:", repr(e))
        client = None
    who = f"{client['name']} ({number})" if client else number
    extra = f" [+{len(media)} attachment{'s' if len(media) > 1 else ''}]" if media else ""
    send_text(config.OWNER_PHONE_NUMBER, f"💬 {who}: {text}{extra}")


def handle(number: str, text: str, media: list):
    """Runs the agent for one incoming message and texts the reply back."""
    annotated = None
    if not media and answer_pending(number, text):
        return
    try:
        images, links, heard = [], [], []
        for url, content_type in media:
            if content_type.startswith("image/"):
                found, saved, note, annotated = read_photo(download(url), content_type)
                images += found
                links += saved
                text += note
            elif content_type.startswith("audio/") or content_type.startswith("video/"):
                heard.append(voice.transcribe(download(url), keywords=get_trade(config.TRADE)["categories"]))

        if heard:
            text = " ".join([text] + heard).strip()
            if not text:
                send_text(number, "🎙️ I couldn't hear anything in that voice note. Please try again.")
                return
        source = "photo" if images else "voice" if heard else "text"
        if annotated:
            source = "shoebox"
        reply = agent.run(text, images=images, history=memory.history(number), source=source, photos=links)
        if heard:
            # Show what was heard, so the owner can check it
            reply = f"🎙️ Heard: \"{' '.join(heard)}\"\n\n{reply}"
        memory.add(number, "user", text + (" [sent a photo]" if images else ""))
        memory.add(number, "assistant", reply)
    except Exception as e:
        print("Agent error:", repr(e))
        reply = "⚠️ Sorry, something went wrong on my side. Please try again in a minute."
    # With several receipts, the reply carries the photo with each one boxed and numbered
    send_text(number, reply, media_url=annotated)


@app.route("/")
def home():
    return "SoloBooks agent is running."


@app.route("/sms", methods=["POST"])
def sms():
    if not is_from_twilio():
        return "Forbidden", 403

    number = request.form.get("From", "").replace("whatsapp:", "")
    resp = MessagingResponse()
    text = request.form.get("Body", "").strip()
    media = [(request.form.get(f"MediaUrl{i}"), request.form.get(f"MediaContentType{i}", ""))
             for i in range(int(request.form.get("NumMedia", 0)))]
    if number != config.OWNER_PHONE_NUMBER:
        # Clients replying to an invoice text: forward to the owner, never run the agent for them
        threading.Thread(target=forward_from_client, args=(number, text, media), daemon=True).start()
        return str(resp)

    if any(t.startswith("image/") for _, t in media):
        resp.message("📸 Got it, working on it...")
    elif media:
        resp.message("🎙️ Got your voice note, working on it...")
    threading.Thread(target=handle, args=(number, text, media), daemon=True).start()
    return str(resp)


@app.route("/photos/<name>")
def photo(name):
    if not photos.NAME_PATTERN.match(name):
        abort(404)
    return send_from_directory(config.PHOTOS_DIR, name)


# --- Invoices --------------------------------------------------------------------

INVOICE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invoice {{ inv.id }} · {{ business }}</title>
<style>
  :root { --ink:#1d2520; --muted:#5f6b63; --line:#dfe5e0; --accent:#0f7a43; --bg:#f6f8f6; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  main { max-width:640px; margin:0 auto; padding:24px 16px 48px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:24px; }
  h1 { font-size:22px; margin:0 0 2px; } .muted { color:var(--muted); font-size:14px; }
  .meta { display:grid; grid-template-columns:auto 1fr; gap:4px 16px; margin:20px 0; font-size:15px; }
  table { width:100%; border-collapse:collapse; margin:8px 0 4px; font-size:15px; }
  th, td { padding:8px 4px; border-bottom:1px solid var(--line); text-align:right; vertical-align:top; }
  th:first-child, td:first-child { text-align:left; } th { font-size:13px; color:var(--muted); font-weight:600; }
  .total td { border-bottom:0; font-weight:700; font-size:18px; padding-top:12px; }
  .actions { display:flex; flex-wrap:wrap; gap:12px; margin-top:24px; }
  .btn { display:inline-block; padding:12px 20px; border-radius:8px; font-weight:600; text-decoration:none; border:0; font-size:16px; cursor:pointer; }
  .pay { background:var(--accent); color:#fff; } .pdf { background:#fff; color:var(--ink); border:1px solid var(--line); }
  .paid { display:inline-block; background:#e3f4ea; color:var(--accent); font-weight:700; padding:6px 12px; border-radius:999px; }
  .note { margin-top:16px; font-size:14px; color:var(--muted); }
</style></head>
<body><main><div class="card">
  <h1>{{ business }}</h1>{% if contact %}<div class="muted">{{ contact }}</div>{% endif %}
  <div class="meta">
    <strong>Invoice</strong><span>{{ inv.id }}</span>
    <strong>Bill to</strong><span>{{ inv.client }}</span>
    <strong>Issued</strong><span>{{ inv.issued }}</span>
    <strong>Due</strong><span>{{ inv.due }}</span>
  </div>
  <table><thead><tr><th>Description</th><th>Qty</th><th>Price</th><th>Amount</th></tr></thead><tbody>
  {% for item in items %}<tr><td>{{ item.description }}</td><td>{{ '%g' % item.quantity }}</td>
    <td>${{ '{:,.2f}'.format(item.unit_price) }}</td><td>${{ '{:,.2f}'.format(item.amount) }}</td></tr>{% endfor %}
  <tr class="total"><td colspan="3">Total</td><td>${{ '{:,.2f}'.format(inv.total) }}</td></tr>
  </tbody></table>
  {% if inv.notes %}<p class="note">{{ inv.notes }}</p>{% endif %}
  <div class="actions">
    {% if inv.status == 'paid' %}<span class="paid">✓ Paid {{ inv.paid_on }}</span>
    {% elif can_pay %}<form method="post" action="/i/{{ inv.token }}/pay"><button class="btn pay">Pay ${{ '{:,.2f}'.format(inv.total) }} now</button></form>{% endif %}
    <a class="btn pdf" href="/i/{{ inv.token }}/pdf">Download PDF</a>
  </div>
  {% if message %}<p class="note">{{ message }}</p>{% endif %}
</div></main></body></html>"""


def _invoice_or_404(token):
    invoice = invoices.find_by_token(token)
    if invoice is None:
        abort(404)
    return invoice


def _render_invoice(invoice, message=None):
    return render_template_string(INVOICE_PAGE, inv=invoice, items=invoices.items_of(invoice),
                                  business=config.BUSINESS_NAME or config.OWNER_NAME, contact=config.BUSINESS_CONTACT,
                                  can_pay=bool(config.STRIPE_SECRET_KEY), message=message)


def record_card_payment(invoice_id: str):
    """Marks an invoice paid by card (once) and texts the owner."""
    invoice, newly_paid = invoices.mark_paid(invoice_id, "card (Stripe)", agent.today(), source="stripe")
    if newly_paid:
        send_text(config.OWNER_PHONE_NUMBER,
                  f"💵 {invoice['client']} paid ${invoice['total']:,.2f} by card ({invoice['id']}). "
                  f"Recorded as income ({invoice['income_id']}).")
    return invoice


@app.route("/i/<token>")
def invoice_page(token):
    return _render_invoice(_invoice_or_404(token))


@app.route("/i/<token>/pdf")
def invoice_pdf(token):
    invoice = _invoice_or_404(token)
    return Response(invoices.pdf(invoice), mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{invoice["id"]}.pdf"'})


@app.route("/i/<token>/pay", methods=["POST"])
def invoice_pay(token):
    invoice = _invoice_or_404(token)
    if invoice.get("status") == "paid":
        return redirect(f"/i/{token}")
    if not config.STRIPE_SECRET_KEY:
        abort(404)
    return redirect(invoices.checkout_url(invoice), code=303)


@app.route("/i/<token>/paid")
def invoice_paid(token):
    """Where Stripe sends the client after paying. We confirm the payment with Stripe ourselves."""
    invoice = _invoice_or_404(token)
    try:
        invoice_id = invoices.check_session(request.args.get("session_id", ""))
    except Exception as e:
        print("Stripe check error:", repr(e))
        invoice_id = None
    if invoice_id != invoice["id"]:
        return _render_invoice(invoice, "We couldn't confirm the payment yet. If you paid, it will show here shortly.")
    return _render_invoice(record_card_payment(invoice_id), "Thank you! Your payment was received.")


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe's notification that a Checkout Session completed (signature-checked)."""
    if not config.STRIPE_WEBHOOK_SECRET:
        abort(404)
    try:
        event = stripe.Webhook.construct_event(request.get_data(), request.headers.get("Stripe-Signature", ""),
                                               config.STRIPE_WEBHOOK_SECRET)
    except Exception:
        return "Bad signature", 400
    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        invoice_id = invoices.paid_invoice_id(event["data"]["object"])
        if invoice_id:
            record_card_payment(invoice_id)
    return "ok"


if __name__ == "__main__":
    app.run(port=8080)
