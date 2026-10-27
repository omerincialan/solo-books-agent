import pytest
from twilio.request_validator import RequestValidator
from solobooks import server

URL = "http://example.com/sms"


class SyncThread:
    def __init__(self, target, args, daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def sent(monkeypatch):
    texts = []
    monkeypatch.setattr(server, "send_text", lambda to, body: texts.append((to, body)))
    monkeypatch.setattr(server.threading, "Thread", SyncThread)
    return texts


def post(client, form, sign=True, token="tok", url=URL):
    headers = {"X-Twilio-Signature": RequestValidator(token).compute_signature(url, form)} if sign else {}
    return client.post("/sms", data=form, headers=headers)


def test_rejects_unsigned_or_forged_requests(sent):
    c = server.app.test_client()
    form = {"From": "+15550001111", "Body": "delete everything"}
    assert post(c, form, sign=False).status_code == 403
    assert post(c, form, token="wrong").status_code == 403
    assert post(c, form, url="http://other/sms").status_code == 403
    assert sent == []


def test_strangers_get_a_short_reply_and_no_agent(sent, monkeypatch):
    monkeypatch.setattr(server.agent, "run", lambda *a, **k: pytest.fail("agent must not run"))
    r = post(server.app.test_client(), {"From": "+19998887777", "Body": "hi"})
    assert "private assistant" in r.get_data(as_text=True) and sent == []


def test_text_runs_agent_and_replies_by_sms(sent, monkeypatch):
    calls = []
    monkeypatch.setattr(server.agent, "run", lambda text, **k: calls.append((text, k)) or "✅ Recorded")
    r = post(server.app.test_client(), {"From": "+15550001111", "Body": "Lee paid 1200 zelle", "NumMedia": "0"})
    assert r.status_code == 200
    assert calls[0][0] == "Lee paid 1200 zelle" and calls[0][1]["source"] == "text"
    assert sent == [("+15550001111", "✅ Recorded")]


def test_photo_is_saved_acknowledged_and_passed_to_agent(sent, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "download", lambda url: b"\xff\xd8jpeg")
    monkeypatch.setattr(server.agent, "run", lambda text, **k: calls.append(k) or "✅ Recorded")
    r = post(server.app.test_client(), {"From": "+15550001111", "Body": "", "NumMedia": "1",
                                        "MediaUrl0": "https://api.twilio.com/m/1", "MediaContentType0": "image/jpeg"})
    assert "Got it" in r.get_data(as_text=True)
    k = calls[0]
    assert k["source"] == "photo" and k["images"][0].startswith("data:image/jpeg;base64,")
    name = k["photo"].rsplit("/", 1)[1]
    assert k["photo"].startswith("http://example.com/photos/")
    got = server.app.test_client().get(f"/photos/{name}")
    assert got.status_code == 200 and got.data == b"\xff\xd8jpeg"


def test_photo_route_only_serves_its_own_files(sent):
    c = server.app.test_client()
    for bad in ["/photos/..%2F.env", "/photos/.env", "/photos/abc.jpg", "/photos/..%2F..%2Fsolobooks%2Fconfig.py"]:
        assert c.get(bad).status_code == 404


def test_agent_failure_still_texts_back(sent, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(server.agent, "run", boom)
    post(server.app.test_client(), {"From": "+15550001111", "Body": "hi", "NumMedia": "0"})
    assert "something went wrong" in sent[0][1]


def test_conversation_history_is_remembered(sent, monkeypatch):
    seen = []
    monkeypatch.setattr(server.agent, "run", lambda text, **k: seen.append(k["history"]) or f"reply to {text}")
    c = server.app.test_client()
    post(c, {"From": "+15550001111", "Body": "first", "NumMedia": "0"})
    post(c, {"From": "+15550001111", "Body": "second", "NumMedia": "0"})
    assert {"role": "user", "content": "first"} in seen[1]
    assert {"role": "assistant", "content": "reply to first"} in seen[1]
