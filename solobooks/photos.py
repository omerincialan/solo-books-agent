"""Receipt photos are kept on the server and linked from the sheet by an unguessable URL."""
import base64
import mimetypes
import os
import re
import secrets
from urllib.parse import urlsplit
from . import config

NAME_PATTERN = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]+$")


def save(image: bytes, content_type: str) -> str:
    """Saves a photo and returns its public link."""
    os.makedirs(config.PHOTOS_DIR, exist_ok=True)
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    name = secrets.token_hex(16) + ext
    with open(os.path.join(config.PHOTOS_DIR, name), "wb") as f:
        f.write(image)
    public = urlsplit(config.PUBLIC_URL)
    return f"{public.scheme}://{public.netloc}/photos/{name}" if public.netloc else f"/photos/{name}"


def data_url(image: bytes, content_type: str) -> str:
    return f"data:{content_type};base64,{base64.b64encode(image).decode()}"
