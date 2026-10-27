"""
Checks the Meta Model API setup with real calls (no Twilio, no sheet):
  python -m scripts.smoke_test
"""
import base64
import json
import struct
import zlib
from solobooks import agent, config


def tiny_png() -> str:
    """A 32x32 white PNG, so the vision check needs no image file."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * 32 for _ in range(32))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


def main():
    print(f"Model: {config.MUSE_MODEL} at {config.META_BASE_URL}")
    if not config.MODEL_API_KEY:
        raise SystemExit("MODEL_API_KEY is not set in .env")

    r = agent.client.chat.completions.create(
        model=config.MUSE_MODEL, messages=[{"role": "user", "content": "Reply with the single word: ready"}])
    print("1. Chat:", r.choices[0].message.content)

    schema = agent._schema(["Fuel", "Other"])
    r = agent.client.chat.completions.create(model=config.MUSE_MODEL, tools=schema, messages=[
        {"role": "system", "content": f"Today is {agent.today()}. Record things with the tools."},
        {"role": "user", "content": "drove 12 miles to the Henderson job, bought gas for 41.20 at Shell, they paid me 400 cash"}])
    calls = r.choices[0].message.tool_calls or []
    print(f"2. Tool calling: {len(calls)} call(s)")
    for call in calls:
        print("   ", call.function.name, json.loads(call.function.arguments))

    r = agent.client.chat.completions.create(model=config.MUSE_MODEL, messages=[{"role": "user", "content": [
        {"type": "text", "text": "What color is this image? One word."},
        {"type": "image_url", "image_url": {"url": tiny_png()}}]}])
    print("3. Vision:", r.choices[0].message.content)


if __name__ == "__main__":
    main()
