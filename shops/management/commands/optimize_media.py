"""Shrink oversized images that were uploaded before upload optimization existed.

Same rules as new uploads (utils/images.py): downscale to the field's limit; JPEG/WebP keep
their format, PNG becomes WebP. A PNG converted to WebP gets a new file name, the row is
updated to point at it, and the old file is removed. Back up MEDIA_ROOT and the database
first.   python manage.py optimize_media [--dry-run]
"""
import os

from django.core.management.base import BaseCommand
from django.db import transaction
from PIL import Image

from accounts.models import User
from orders.models import PhotoProof
from shops.models import Shop
from utils.images import EXTENSION, encode_optimized

# (model, field, max px on the longest side) — mirrors the limits applied on upload.
TARGETS = [
    (Shop, 'logo', 800), (Shop, 'banner', 1920), (Shop, 'owner_selfie_with_id', 2000),
    (User, 'profile_picture', 600), (PhotoProof, 'photo', 1920),
]


class Command(BaseCommand):
    help = 'Downscale/re-encode oversized uploaded images (PNG -> WebP).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, dry_run=False, **opts):
        before = after = changed = 0
        done = {}                                   # old path -> new name (files shared by several rows)
        for model, field, max_px in TARGETS:
            for obj in model.objects.exclude(**{f'{field}__isnull': True}).exclude(**{field: ''}):
                f = getattr(obj, field)
                try:
                    path = f.path
                except Exception:
                    continue
                if path in done:
                    new_name = done[path]
                else:
                    if not os.path.exists(path):
                        continue
                    size = os.path.getsize(path)
                    try:
                        with Image.open(path) as im:
                            if max(im.size) <= max_px and size < 200 * 1024:
                                continue
                            result = encode_optimized(im, max_px)
                    except Exception as exc:
                        self.stderr.write(f'skip {f.name}: {exc}')
                        continue
                    if not result or len(result[0]) >= size:
                        continue
                    data, fmt = result
                    new_name = os.path.splitext(f.name)[0] + EXTENSION[fmt]
                    new_path = os.path.splitext(path)[0] + EXTENSION[fmt]
                    before += size
                    after += len(data)
                    changed += 1
                    self.stdout.write(f'{f.name} -> {new_name}: {size // 1024} KB -> {len(data) // 1024} KB')
                    done[path] = new_name
                    if not dry_run:
                        with open(new_path, 'wb') as fh:
                            fh.write(data)
                if dry_run or new_name == f.name:
                    continue
                with transaction.atomic():
                    model.objects.filter(pk=obj.pk).update(**{field: new_name})
        if not dry_run:
            for old_path, new_name in done.items():
                if not old_path.endswith(os.path.splitext(new_name)[1]) and os.path.exists(old_path):
                    os.remove(old_path)             # replaced by the converted file
        prefix = '[dry run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}{changed} files optimized: {before / 1048576:.1f} MB -> {after / 1048576:.1f} MB'))
