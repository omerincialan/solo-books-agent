import io
import asyncio
import pytest
from PIL import Image, ImageDraw
from solobooks import agent, books, server, shoebox
from tests.fakes import FakeSheet, ScriptedModel
from tests.test_server import SyncThread, post

# Three receipts on a dark table: (left, top, right, bottom)
RECEIPTS = [(60, 80, 360, 700), (440, 60, 760, 640), (840, 100, 1140, 760)]


def receipts_photo(count=3, exif_rotate=False) -> bytes:
    img = Image.new("RGB", (1200, 900), (60, 45, 35))
    draw = ImageDraw.Draw(img)
    for box in RECEIPTS[:count]:
        draw.rectangle(box, fill=(250, 250, 245))
        draw.text((box[0] + 20, box[1] + 20), "STORE\\nTOTAL $12.00", fill="black")
    out = io.BytesIO()
    if exif_rotate:
        exif = Image.Exif()
        exif[0x0112] = 6  # "rotate 90° clockwise to display", as phones write it
        img.save(out, "JPEG", exif=exif)
    else:
        img.save(out, "JPEG")
    return out.getvalue()


def size(jpeg):
    return Image.open(io.BytesIO(jpeg)).size


def test_prepare_applies_phone_rotation_and_shrinks_big_photos():
    assert size(shoebox.prepare(receipts_photo(exif_rotate=True))) == (900, 1200)
    big = io.BytesIO()
    Image.new("RGB", (6000, 4000), "white").save(big, "JPEG")
    assert max(size(shoebox.prepare(big.getvalue()))) == shoebox.MAX_SIDE


def test_clean_boxes_drops_specks_and_duplicates_and_orders_by_row():
    boxes = [RECEIPTS[2], RECEIPTS[0], (65, 85, 355, 690),   # near-duplicate of receipt 1
             (500, 800, 510, 810),                           # a speck
             RECEIPTS[1]]
    assert shoebox.clean_boxes(boxes, 1200, 900) == RECEIPTS


def test_two_rows_are_numbered_top_row_first():
    top_right, top_left = (700, 50, 900, 400), (100, 80, 300, 420)
    bottom_left, bottom_right = (120, 500, 320, 850), (650, 480, 850, 830)
    boxes = [bottom_right, top_right, bottom_left, top_left]
    assert shoebox.clean_boxes(boxes, 1000, 900) == [top_left, top_right, bottom_left, bottom_right]


def test_cut_out_and_annotate():
    jpeg = shoebox.prepare(receipts_photo())
    crops = shoebox.cut_out(jpeg, RECEIPTS)
    assert len(crops) == 3
    w, h = size(crops[0])
    assert 300 <= w <= 320 and 620 <= h <= 660          # receipt plus a small margin
    assert size(shoebox.annotate(jpeg, RECEIPTS)) == (1200, 900)


def test_sam_request_matches_the_meta_cookbook(monkeypatch):
    seen = {}

    class FakeResponses:
        async def create(self, **kw):
            seen.update(kw)
            raise RuntimeError("stop after capturing the request")

    class FakeAsyncOpenAI:
        def __init__(self, base_url, api_key):
            seen["base_url"] = base_url
            self.responses = FakeResponses()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(shoebox, "AsyncOpenAI", FakeAsyncOpenAI)
    with pytest.raises(RuntimeError, match="stop after"):
        asyncio.run(shoebox._segment(shoebox.prepare(receipts_photo())))
    assert seen["base_url"] == "https://api.meta.ai/v1" and seen["model"] == "sam-3.1" and seen["stream"] is True
    content = seen["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "receipt"}
    assert content[1]["type"] == "input_image" and content[1]["image_url"].startswith("data:image/jpeg;base64,")


@pytest.fixture
def sent(monkeypatch):
    texts = []
    monkeypatch.setattr(server, "send_text", lambda to, body, media_url=None: texts.append((body, media_url)))
    monkeypatch.setattr(server.threading, "Thread", SyncThread)
    monkeypatch.setattr(server, "download", lambda url: receipts_photo())
    return texts


PHOTO = {"From": "+15550001111", "Body": "", "NumMedia": "1",
         "MediaUrl0": "https://api.twilio.com/m/1", "MediaContentType0": "image/jpeg"}


def test_shoebox_photo_becomes_one_image_per_receipt(sent, monkeypatch):
    calls = []
    monkeypatch.setattr(server.shoebox, "find_receipts", lambda jpeg: RECEIPTS)
    monkeypatch.setattr(server.agent, "run", lambda text, **k: calls.append((text, k)) or "✅ Recorded 3 receipts")
    post(server.app.test_client(), PHOTO)

    text, k = calls[0]
    assert k["source"] == "shoebox" and len(k["images"]) == 3 and len(k["photos"]) == 3
    assert "contains 3 receipts" in text and "receipt_number" in text
    body, media_url = sent[0]
    assert body == "✅ Recorded 3 receipts"
    annotated = server.app.test_client().get("/photos/" + media_url.rsplit("/", 1)[1])
    assert annotated.status_code == 200 and size(annotated.data) == (1200, 900)


def test_sam_failure_falls_back_to_the_whole_photo(sent, monkeypatch):
    calls = []
    def boom(jpeg):
        raise RuntimeError("SAM is down")
    monkeypatch.setattr(server.shoebox, "find_receipts", boom)
    monkeypatch.setattr(server.agent, "run", lambda text, **k: calls.append(k) or "✅ Recorded")
    post(server.app.test_client(), PHOTO)
    assert calls[0]["source"] == "photo" and len(calls[0]["images"]) == 1
    assert sent[0] == ("✅ Recorded", None)


def test_each_expense_links_to_its_own_cut_out(monkeypatch):
    monkeypatch.setattr(books, "_sheet", FakeSheet())
    monkeypatch.setattr(books, "_tabs", {})
    monkeypatch.setattr(agent, "client", ScriptedModel([
        [("record_expense", {"date": "2026-10-28", "vendor": "Shell", "total": 61.2, "category": "Fuel", "receipt_number": 2}),
         ("record_expense", {"date": "2026-10-27", "vendor": "Ace", "total": 23, "category": "Equipment & parts", "receipt_number": 1})],
        "✅ Recorded 2 receipts"]))
    agent.run("", images=["a", "b"], source="shoebox", photos=["http://x/photos/one.jpg", "http://x/photos/two.jpg"])
    photo_of = {e["vendor"]: e["photo"] for e in books.entries("expense")}
    assert photo_of == {"Shell": "http://x/photos/two.jpg", "Ace": "http://x/photos/one.jpg"}
