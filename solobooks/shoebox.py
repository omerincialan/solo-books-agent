"""
Shoebox mode: one photo of a pile of receipts. SAM 3.1 finds each receipt,
we cut them out, and Muse Spark reads every cut-out as its own expense.
"""
import asyncio
import io
import os
from contextlib import aclosing
from PIL import Image, ImageDraw, ImageFont, ImageOps
from openai import AsyncOpenAI
from meta_sam_parser import image_segmentation_format, parse_responses_stream
from . import config

SAM_MODEL = os.getenv("SAM_MODEL", "sam-3.1")
CONCEPT = "receipt"
MAX_SIDE = 2048            # photos are scaled down to this before anything else
MIN_AREA = 0.01            # ignore detections smaller than 1% of the photo
MIN_CONFIDENCE = 0.3       # ignore detections SAM itself isn't sure about
PADDING = 0.03             # margin added around each cut-out


def prepare(image: bytes) -> bytes:
    """Applies the phone's rotation (EXIF), scales the photo down and re-encodes it as JPEG,
    so SAM's box coordinates and our crops refer to the same pixels."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image))).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


async def _events(stream):
    """Plain event dicts for the parser. The API streams response.content_part.done but not
    response.output_text.done, which the parser needs to close a text lane, so we add it
    (same approach as Meta's SAM cookbook)."""
    done = set()
    async for event in stream:
        data = event.model_dump() if hasattr(event, "model_dump") else dict(event)
        lane = (data.get("item_id"), data.get("output_index"), data.get("content_index"))
        if data.get("type") == "response.output_text.done":
            if lane not in done:
                done.add(lane)
                yield data
            continue
        yield data
        part = data.get("part")
        if (data.get("type") == "response.content_part.done" and lane not in done
                and isinstance(part, dict) and part.get("type") == "output_text"):
            done.add(lane)
            yield {"type": "response.output_text.done", "item_id": lane[0], "output_index": lane[1],
                   "content_index": lane[2], "text": part.get("text", "")}


async def _segment(jpeg: bytes) -> list:
    from .photos import data_url
    async with AsyncOpenAI(base_url=config.META_BASE_URL, api_key=config.MODEL_API_KEY or "not-set") as api:
        stream = await api.responses.create(model=SAM_MODEL, stream=True, input=[{
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": CONCEPT},
                        {"type": "input_image", "image_url": data_url(jpeg, "image/jpeg")}]}])
        async with stream, aclosing(_events(stream)) as events:
            result = await parse_responses_stream(events, image_segmentation_format()).final_result()
    if result.outcome.status != "completed":
        raise RuntimeError(f"SAM did not finish: {getattr(result.outcome, 'reason', 'unknown')}")
    # Boxes are in pixels of the photo we sent ("source-media coordinates")
    return [(r.left, r.top, r.right, r.bottom) for r in result.records
            if r.kind == "box" and (r.confidence is None or r.confidence >= MIN_CONFIDENCE)]


def _overlap(a, b) -> float:
    """Intersection over the smaller box."""
    w = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (w * h) / smaller if smaller else 0


def clean_boxes(boxes: list, width: int, height: int) -> list:
    """Drops tiny detections and duplicates, and orders receipts top-to-bottom, left-to-right."""
    kept = []
    for box in sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True):
        area = (box[2] - box[0]) * (box[3] - box[1])
        if area >= MIN_AREA * width * height and all(_overlap(box, k) < 0.6 for k in kept):
            kept.append(box)
    # Number them like reading a page: rows from the top, left to right within a row. A receipt
    # starts a new row when its top is below the middle of the row's first receipt.
    rows = []
    for box in sorted(kept, key=lambda b: b[1]):
        first = rows[-1][0] if rows else None
        if first and box[1] < (first[1] + first[3]) / 2:
            rows[-1].append(box)
        else:
            rows.append([box])
    return [box for row in rows for box in sorted(row, key=lambda b: b[0])]


def find_receipts(jpeg: bytes) -> list:
    """Boxes (left, top, right, bottom) of the receipts in a prepared photo."""
    width, height = Image.open(io.BytesIO(jpeg)).size
    return clean_boxes(asyncio.run(_segment(jpeg)), width, height)


def cut_out(jpeg: bytes, boxes: list) -> list:
    """One JPEG per receipt, with a small margin."""
    img = Image.open(io.BytesIO(jpeg))
    crops = []
    for left, top, right, bottom in boxes:
        pad_x, pad_y = (right - left) * PADDING, (bottom - top) * PADDING
        crop = img.crop((max(0, left - pad_x), max(0, top - pad_y),
                         min(img.width, right + pad_x), min(img.height, bottom + pad_y)))
        out = io.BytesIO()
        crop.save(out, "JPEG", quality=92)
        crops.append(out.getvalue())
    return crops


def annotate(jpeg: bytes, boxes: list) -> bytes:
    """The photo with each found receipt boxed and numbered, to text back to the owner."""
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    draw = ImageDraw.Draw(img)
    line = max(4, img.width // 250)
    try:
        font = ImageFont.load_default(size=max(24, img.width // 25))
    except TypeError:  # older Pillow
        font = ImageFont.load_default()
    for number, (left, top, right, bottom) in enumerate(boxes, start=1):
        draw.rectangle((left, top, right, bottom), outline=(0, 200, 90), width=line)
        label = str(number)
        x0, y0, x1, y1 = draw.textbbox((left + line, top + line), label, font=font)
        draw.rectangle((x0 - line, y0 - line, x1 + line, y1 + line), fill=(0, 200, 90))
        draw.text((left + line, top + line), label, fill="white", font=font)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()
