"""One-time cleanup: resize all existing product variant images.

Usage:
    python manage.py resize_existing_images           # actually resize
    python manage.py resize_existing_images --dry-run # show what would change
    python manage.py resize_existing_images --max-size 800
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Resize existing ProductVariant images to a max bounding box"

    def add_arguments(self, parser):
        parser.add_argument("--max-size", type=int, default=1200,
            help="Max width or height in pixels (default 1200)")
        parser.add_argument("--quality", type=int, default=80,
            help="JPEG quality 1-100 (default 80)")
        parser.add_argument("--dry-run", action="store_true",
            help="Don't actually modify files; just report what would change")

    def handle(self, *args, **opts):
        import os
        from inventory.models import ProductVariant, _resize_image_in_place

        max_size = opts["max_size"]
        quality = opts["quality"]
        dry_run = opts["dry_run"]

        variants = ProductVariant.objects.exclude(image="").exclude(image=None)
        total_before = 0
        total_after = 0
        n_resized = 0
        n_skipped = 0
        n_errors = 0

        for v in variants:
            try:
                if not v.image:
                    continue
                path = v.image.path
                if not os.path.isfile(path):
                    n_skipped += 1
                    continue
                size_before = os.path.getsize(path)
                total_before += size_before
                if dry_run:
                    self.stdout.write(f"  Would resize: {path} ({size_before/1024:.0f} KB)")
                    n_resized += 1
                    continue
                _resize_image_in_place(path, max_size=max_size, quality=quality)
                size_after = os.path.getsize(path)
                total_after += size_after
                pct = (1 - size_after / size_before) * 100 if size_before else 0
                if pct > 1:
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✓ {path}: {size_before/1024:.0f} KB → {size_after/1024:.0f} KB (-{pct:.0f}%)"
                    ))
                    n_resized += 1
                else:
                    n_skipped += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ {v.image} : {e}"))
                n_errors += 1

        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — {n_resized} would be resized, {n_skipped} already small, {n_errors} errors"
            ))
        else:
            saved = total_before - total_after
            self.stdout.write(self.style.SUCCESS(
                f"Done — {n_resized} resized, {n_skipped} skipped, {n_errors} errors. "
                f"Saved {saved/1024/1024:.1f} MB total ({total_before/1024/1024:.1f} MB → {total_after/1024/1024:.1f} MB)."
            ))
