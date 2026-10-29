import pytest
from solobooks import agent, books, config, invoices, memory, server
from scripts import daily_nudge
from tests.fakes import FakeSheet, ScriptedModel
from tests.test_server import SyncThread

TODAY = "2026-10-29"
ITEMS = [{"description": "Oak removal", "quantity": 6, "unit_price": 95},
         {"description": "Haul-away", "unit_price": 120}]


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(books, "_sheet", FakeSheet())
    monkeypatch.setattr(books, "_tabs", {})
    monkeypatch.setattr(agent, "today", lambda: TODAY)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "BUSINESS_NAME", "Dana's Tree Care")


@pytest.fixture
def sent(monkeypatch):
    texts = []
    monkeypatch.setattr(server, "send_text", lambda to, body, media_url=None: texts.append((to, body)))
    monkeypatch.setattr(server.threading, "Thread", SyncThread)
    return texts


def test_create_computes_totals_numbers_and_due_date():
    first = invoices.create("Hendersons", ITEMS, TODAY)
    assert first["id"] == "INV-0001" and first["total"] == 690.0
    assert first["due"] == "2026-11-12"                                     # 14 days by default
    assert first["link"] == f"http://example.com/i/{first['token']}" and len(first["token"]) >= 20
    assert [i["amount"] for i in invoices.items_of(first)] == [570.0, 120.0]
    second = invoices.create("Lee", [{"description": "Pruning", "quantity": 2.5, "unit_price": 33.33}], TODAY,
                             due_date="2026-11-30")
    assert second["id"] == "INV-0002" and second["total"] == 83.33 and second["due"] == "2026-11-30"
    assert books.kind_of("INV-0002") == "invoice" and books.kind_of("I1A2B") == "income"


@pytest.mark.parametrize("quantity, price, expected", [(2.5, 33.33, 83.33), (3, 0.1, 0.3), (1, 19.995, 20.0), (7, 14.29, 100.03)])
def test_money_is_exact(quantity, price, expected):
    invoice = invoices.create("Lee", [{"description": "Work", "quantity": quantity, "unit_price": price}], TODAY)
    assert invoice["total"] == expected


@pytest.mark.parametrize("bad", [[], [{"description": "", "unit_price": 10}], [{"description": "x", "unit_price": 0}],
                                 [{"description": "x", "quantity": -1, "unit_price": 10}]])
def test_invalid_line_items_are_rejected(bad):
    with pytest.raises(ValueError):
        invoices.create("Lee", bad, TODAY)


def test_payment_is_recorded_exactly_once():
    invoice = invoices.create("Hendersons", ITEMS, TODAY)
    paid, newly = invoices.mark_paid(invoice["id"], "card (Stripe)", TODAY, "stripe")
    assert newly and paid["status"] == "paid" and paid["paid_on"] == TODAY
    again, newly_again = invoices.mark_paid(invoice["id"], "card (Stripe)", TODAY, "stripe")
    assert not newly_again
    income = books.entries("income")
    assert len(income) == 1 and income[0]["amount"] == 690.0 and income[0]["id"] == paid["income_id"]


def test_open_invoices_aging_and_total_owed():
    invoices.create("Hendersons", ITEMS, "2026-10-01", due_date="2026-10-23")      # 6 days overdue
    invoices.create("Lee", [{"description": "Stump grinding", "unit_price": 1200}], "2026-08-01",
                    due_date="2026-08-15")                                         # 75 days overdue
    invoices.create("Ortiz", [{"description": "Trim", "unit_price": 300}], TODAY)  # not due yet
    paid = invoices.create("Kim", [{"description": "Trim", "unit_price": 50}], TODAY)
    invoices.mark_paid(paid["id"], "cash", TODAY, "text")
    summary = invoices.open_invoices(TODAY)
    assert [(i["client"], i["days_overdue"]) for i in summary["open_invoices"]] == [("Lee", 75), ("Hendersons", 6), ("Ortiz", -14)]
    assert summary["total_owed"] == 2190.0
    assert summary["aging"] == {"61-90 days overdue": 1200.0, "1-30 days overdue": 690.0, "not due yet": 300.0}


def test_pdf_is_a_real_pdf_even_with_non_latin_text():
    invoice = invoices.create("Nguyễn Café ☕", ITEMS, TODAY, notes="Thank you — Dana")
    data = invoices.pdf(invoice)
    assert data.startswith(b"%PDF") and len(data) > 1000


def test_checkout_session_uses_cents_and_links_back_to_the_invoice(monkeypatch):
    seen = {}
    monkeypatch.setattr(invoices.stripe.checkout.Session, "create",
                        lambda **kw: seen.update(kw) or type("S", (), {"url": "https://checkout.stripe.com/c/x"})())
    invoice = invoices.create("Hendersons", ITEMS, TODAY)
    assert invoices.checkout_url(invoice) == "https://checkout.stripe.com/c/x"
    assert [li["price_data"]["unit_amount"] for li in seen["line_items"]] == [57000, 12000]
    assert seen["metadata"] == {"invoice_id": "INV-0001"} and seen["mode"] == "payment"
    assert seen["success_url"].endswith(f"/i/{invoice['token']}/paid?session_id={{CHECKOUT_SESSION_ID}}")


def test_only_a_paid_session_counts():
    assert invoices.paid_invoice_id({"payment_status": "paid", "metadata": {"invoice_id": "INV-0001"}}) == "INV-0001"
    assert invoices.paid_invoice_id({"payment_status": "unpaid", "metadata": {"invoice_id": "INV-0001"}}) is None


def test_invoice_page_pdf_and_unknown_links(monkeypatch):
    invoice = invoices.create("Hendersons", ITEMS, TODAY)
    c = server.app.test_client()
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "")
    page = c.get(f"/i/{invoice['token']}").get_data(as_text=True)
    assert "INV-0001" in page and "Oak removal" in page and "$690.00" in page and "Download PDF" in page
    assert "Pay $" not in page                                   # no Stripe key: no pay button
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_x")
    assert "Pay $690.00 now" in c.get(f"/i/{invoice['token']}").get_data(as_text=True)
    pdf = c.get(f"/i/{invoice['token']}/pdf")
    assert pdf.mimetype == "application/pdf" and pdf.data.startswith(b"%PDF")
    assert c.get("/i/not-a-real-token").status_code == 404


def test_pay_button_sends_the_client_to_stripe(monkeypatch):
    invoice = invoices.create("Hendersons", ITEMS, TODAY)
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(invoices, "checkout_url", lambda inv: "https://checkout.stripe.com/c/abc")
    r = server.app.test_client().post(f"/i/{invoice['token']}/pay")
    assert r.status_code == 303 and r.headers["Location"] == "https://checkout.stripe.com/c/abc"


def test_return_from_stripe_marks_paid_and_texts_owner_once(sent, monkeypatch):
    invoice = invoices.create("Hendersons", ITEMS, TODAY)
    monkeypatch.setattr(invoices, "check_session", lambda sid: "INV-0001" if sid == "cs_good" else None)
    c = server.app.test_client()
    assert "confirm the payment yet" in c.get(f"/i/{invoice['token']}/paid?session_id=cs_fake").get_data(as_text=True)
    assert invoices.find("INV-0001")["status"] == "open" and sent == []

    page = c.get(f"/i/{invoice['token']}/paid?session_id=cs_good").get_data(as_text=True)
    assert "Thank you" in page and "✓ Paid" in page
    c.get(f"/i/{invoice['token']}/paid?session_id=cs_good")             # refresh: nothing new
    assert len(sent) == 1 and "Hendersons paid $690.00 by card (INV-0001)" in sent[0][1]
    assert len(books.entries("income")) == 1


def test_webhook_needs_a_valid_signature(sent, monkeypatch):
    invoice = invoices.create("Hendersons", ITEMS, TODAY)
    c = server.app.test_client()
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "")
    assert c.post("/stripe/webhook", data="{}").status_code == 404              # not configured
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    assert c.post("/stripe/webhook", data="{}", headers={"Stripe-Signature": "bad"}).status_code == 400
    event = {"type": "checkout.session.completed",
             "data": {"object": {"payment_status": "paid", "metadata": {"invoice_id": invoice["id"]}}}}
    monkeypatch.setattr(server.stripe.Webhook, "construct_event", lambda payload, sig, secret: event)
    assert c.post("/stripe/webhook", data="{}", headers={"Stripe-Signature": "t=1,v1=x"}).status_code == 200
    assert invoices.find(invoice["id"])["status"] == "paid" and len(sent) == 1


def test_agent_creates_invoices_and_closes_them_on_payment(monkeypatch):
    model = ScriptedModel([
        [("create_invoice", {"client": "Hendersons", "items": ITEMS, "due_in_days": 14})],
        lambda results: f"🧾 {results[0]['id']} ${results[0]['total']:.2f} due {results[0]['due']}: {results[0]['link']}",
    ])
    monkeypatch.setattr(agent, "client", model)
    reply = agent.run("Invoice the Hendersons for the oak removal: 6 hours at 95, haul-away 120, due in 2 weeks")
    assert reply.startswith("🧾 INV-0001 $690.00 due 2026-11-12: http://example.com/i/")
    tools = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert {"create_invoice", "list_invoices", "mark_invoice_paid"} <= tools

    monkeypatch.setattr(agent, "client", ScriptedModel([
        [("list_invoices", {})], [("mark_invoice_paid", {"invoice_id": "INV-0001", "paid_with": "check"})], "💵 Done"]))
    agent.run("the Hendersons paid the 690 by check")
    assert invoices.find("INV-0001")["status"] == "paid"
    assert books.entries("income")[0]["paid_with"] == "check"


def test_daily_nudge_reminds_once_every_few_days(sent, monkeypatch):
    invoices.create("Lee", [{"description": "Stump grinding", "unit_price": 1200}], "2026-08-01", due_date="2026-10-08")
    invoices.create("Ortiz", [{"description": "Trim", "unit_price": 300}], TODAY)            # not due: no reminder
    link = invoices.find("INV-0001")["link"]
    monkeypatch.setattr(agent, "client", ScriptedModel([f"Hi Lee, invoice INV-0001 for $1,200 is past due. Pay: {link}"]))
    daily_nudge.main()
    assert len(sent) == 1 and "INV-0001 Lee $1,200.00, 21 days overdue" in sent[0][1] and link in sent[0][1]
    assert "Ortiz" not in sent[0][1] and invoices.find("INV-0001")["last_reminder"] == TODAY
    daily_nudge.main()                                                                       # same day: quiet
    assert len(sent) == 1


def test_reminder_falls_back_to_a_template_without_the_link(monkeypatch):
    invoice = {"id": "INV-0009", "client": "Lee", "total": 50.0, "due": "2026-10-01", "days_overdue": 28,
               "link": "http://example.com/i/tok"}
    monkeypatch.setattr(agent, "client", ScriptedModel(["Hi Lee, please pay soon."]))          # dropped the link
    assert "http://example.com/i/tok" in daily_nudge.draft_reminder(invoice)
