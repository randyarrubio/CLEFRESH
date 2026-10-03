"""Shrink oversized uploaded images before they're stored.

Phones upload 3–12 MP photos (often 2–5 MB) that the site only ever shows at a few hundred
pixels. Each new upload is downscaled to `max_px` on its longest side and re-encoded:
JPEG and WebP keep their format; PNG (lossless, so photos stay huge) becomes WebP, which
keeps transparency. Validation still runs first (utils.validators), and anything PIL
can't handle is stored untouched.
"""
import io
import logging
import os

from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

SAVE_OPTIONS = {
    'JPEG': {'quality': 85, 'optimize': True, 'progressive': True},
    'WEBP': {'quality': 82, 'method': 6},
}
OUTPUT_FORMAT = {'JPEG': 'JPEG', 'WEBP': 'WEBP', 'PNG': 'WEBP'}
EXTENSION = {'JPEG': '.jpg', 'WEBP': '.webp'}


def encode_optimized(im, max_px):
    """Return (bytes, output_format) for a PIL image downscaled to max_px, or None if unsupported."""
    from PIL import Image, ImageOps
    out_fmt = OUTPUT_FORMAT.get(im.format)
    if not out_fmt:
        return None
    im = ImageOps.exif_transpose(im)              # keep phone photos upright once EXIF is dropped
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px), Image.LANCZOS)
    if out_fmt == 'JPEG' and im.mode not in ('RGB', 'L'):
        im = im.convert('RGB')
    elif out_fmt == 'WEBP' and im.mode not in ('RGB', 'RGBA'):
        im = im.convert('RGBA' if 'transparency' in im.info or im.mode in ('LA', 'PA') else 'RGB')
    buf = io.BytesIO()
    im.save(buf, out_fmt, **SAVE_OPTIONS[out_fmt])
    return buf.getvalue(), out_fmt


def shrink_uploaded_image(fieldfile, max_px=1600, min_bytes=200 * 1024):
    """Optimize a just-uploaded (uncommitted) ImageField file. No-op for already-stored files."""
    if not fieldfile or getattr(fieldfile, '_committed', True):
        return
    upload = fieldfile.file
    try:
        from PIL import Image
        upload.seek(0)
        im = Image.open(upload)
        size = getattr(upload, 'size', 0) or 0
        if max(im.size) <= max_px and size < min_bytes:
            return
        result = encode_optimized(im, max_px)
        if not result or len(result[0]) >= size:
            upload.seek(0)
            return                                 # re-encoding didn't help; keep the original
        data, fmt = result
        name = os.path.splitext(fieldfile.name)[0] + EXTENSION[fmt]   # upload_to builds the UUID name from this
        fieldfile.file = ContentFile(data, name=name)
        fieldfile.name = name
        fieldfile._committed = False
    except Exception:
        logger.exception('Could not optimize uploaded image %s', getattr(fieldfile, 'name', '?'))
        try:
            upload.seek(0)
        except Exception:
            pass
