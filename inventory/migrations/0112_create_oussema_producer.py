import os
from django.db import migrations
from django.contrib.auth.hashers import make_password


def create_oussema(apps, schema_editor):
    """Create the 'oussema' Producteur account alongside the existing producer(s)
    so production items can be assigned to — and viewed under — him.

    Idempotent (safe to re-run). No password is stored in code: a temporary one
    is read from the OUSSEMA_INIT_PASSWORD env var when set (and must be changed
    on first login); otherwise the account is created WITHOUT a usable password
    and an admin sets one later in Django admin. Either way the account exists as
    a 'producer', which is all the producer picker / assignment needs.
    """
    User = apps.get_model("auth", "User")
    UserProfile = apps.get_model("inventory", "UserProfile")

    user, created = User.objects.get_or_create(
        username="oussema",
        defaults={"is_active": True, "is_staff": False, "is_superuser": False},
    )
    raw = (os.environ.get("OUSSEMA_INIT_PASSWORD") or "").strip()
    if created:
        # make_password(None) -> an unusable password (cannot log in until set).
        user.password = make_password(raw) if raw else make_password(None)
        user.save(update_fields=["password"])

    prof, _ = UserProfile.objects.get_or_create(user=user)
    prof.role = "producer"
    if created and raw:
        # Only force a password change when we actually set a temp password.
        prof.must_change_password = True
    prof.save()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0111_coucou_femme_sales_page"),
    ]

    operations = [
        migrations.RunPython(create_oussema, noop),
    ]
