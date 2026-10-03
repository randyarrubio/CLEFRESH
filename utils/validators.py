from pathlib import Path

from django.core.exceptions import ValidationError


MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_DOCUMENT_SIZE = 10 * 1024 * 1024
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
DOCUMENT_EXTENSIONS = IMAGE_EXTENSIONS | {'.pdf'}
PDF_MAGIC = b'%PDF'


def validate_image_upload(file_obj):
    _validate_upload(file_obj, IMAGE_EXTENSIONS, MAX_IMAGE_SIZE, 'image')
    _validate_image_content(file_obj)


def validate_document_upload(file_obj):
    _validate_upload(file_obj, DOCUMENT_EXTENSIONS, MAX_DOCUMENT_SIZE, 'document')
    suffix = Path(file_obj.name or '').suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        _validate_image_content(file_obj)
    elif suffix == '.pdf':
        _validate_pdf_magic(file_obj)


def _validate_upload(file_obj, allowed_extensions, max_size, label):
    suffix = Path(file_obj.name or '').suffix.lower()
    if suffix not in allowed_extensions:
        allowed = ', '.join(sorted(allowed_extensions))
        raise ValidationError(f'Unsupported {label} file type. Allowed: {allowed}.')
    if file_obj.size and file_obj.size > max_size:
        max_mb = max_size // (1024 * 1024)
        raise ValidationError(f'{label.title()} file is too large. Maximum size is {max_mb} MB.')


def _validate_image_content(file_obj):
    try:
        from PIL import Image
        file_obj.seek(0)
        img = Image.open(file_obj)
        img.verify()
        file_obj.seek(0)
    except ValidationError:
        raise
    except Exception:
        try:
            file_obj.seek(0)
        except Exception:
            pass
        raise ValidationError('File does not appear to be a valid image.')


def _validate_pdf_magic(file_obj):
    try:
        file_obj.seek(0)
        header = file_obj.read(4)
        file_obj.seek(0)
    except Exception:
        raise ValidationError('Could not read file.')
    if header != PDF_MAGIC:
        raise ValidationError('File does not appear to be a valid PDF.')
