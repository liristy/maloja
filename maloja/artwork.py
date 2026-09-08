"""Image encoding and stable identities, independent of the server/database."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unicodedata
from threading import Lock

from PIL import Image, ImageOps

_thumbnail_locks = [Lock() for _ in range(32)]


def normalize(value):
    # Preserve punctuation and Chinese characters: stripping them causes collisions.
    return unicodedata.normalize("NFC", value).strip().casefold()


def identity(kind, entity):
    if kind == "artist":
        value = [kind, normalize(entity)]
    else:
        value = [kind, sorted({normalize(a) for a in entity.get("artists") or []}),
                 normalize(entity["title" if kind == "track" else "albumtitle"])]
        if kind == "track" and entity.get("album"):
            value.append(identity("album", entity["album"]))
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".artwork-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def encode_image(source, size=320, quality=78):
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
        output = io.BytesIO()
        image.save(output, format="WEBP", quality=quality, method=4)
        return output.getvalue()


def cache_image(source, cache_dir, size=320, quality=78):
    """Cache by source revision and encoder settings; never change the library."""
    source = Path(source).resolve(strict=True)
    stat = source.stat()
    revision = f"v1:{source}:{stat.st_mtime_ns}:{stat.st_size}:{size}:{quality}"
    name = hashlib.sha256(revision.encode()).hexdigest() + ".webp"
    target = Path(cache_dir) / name
    with _thumbnail_locks[int(name[:8], 16) % len(_thumbnail_locks)]:
        if not target.exists():
            atomic_write(target, encode_image(source, size, quality))
    return "/cacheimages/" + name
