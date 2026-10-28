"""
Twilio SMS/MMS webhook. Twilio only waits ~15 seconds for an answer, so the
webhook returns right away and the agent's reply is sent as a new text once
the work is done.
"""
import threading
import requests
from flask import Flask, request, send_from_directory, abort
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from . import agent, config, memory, photos, shoebox, voice
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
        boxes = shoebox.find_receipts(jpeg)
    except Exception as e:
        print("SAM error, using the whole photo:", repr(e))
        boxes = []
    if len(boxes) < 2:
        return [photos.data_url(jpeg, "image/jpeg")], [photos.save(jpeg, "image/jpeg")], "", None

    crops = shoebox.cut_out(jpeg, boxes)
    links = [photos.save(crop, "image/jpeg") for crop in crops]
    annotated = photos.save(shoebox.annotate(jpeg, boxes), "image/jpeg")
    note = (f"\n(This photo contains {len(crops)} receipts. They were cut out with SAM and sent as images "
            f"1 to {len(crops)}: record each one with its receipt_number.)")
    return [photos.data_url(c, "image/jpeg") for c in crops], links, note, annotated


def handle(number: str, text: str, media: list):
    """Runs the agent for one incoming message and texts the reply back."""
    annotated = None
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
    if number != config.OWNER_PHONE_NUMBER:
        resp.message("This number is a private assistant.")
        return str(resp)

    text = request.form.get("Body", "").strip()
    media = [(request.form.get(f"MediaUrl{i}"), request.form.get(f"MediaContentType{i}", ""))
             for i in range(int(request.form.get("NumMedia", 0)))]

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


if __name__ == "__main__":
    app.run(port=8080)
