"""Clean fancy-unicode / emoji customer names already stored.

Social-media display names often use stylized unicode (bold/fraktur letters)
and emoji, which Navex rejects and which look wrong on labels. New orders are
cleaned on import; this one-time pass folds existing Order.customer_name and
Customer.name to plain letters and strips emoji. Only rows that actually change
are written. Normal Latin/Arabic names are left untouched.
"""
import unicodedata

from django.db import migrations


def _clean(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "M", "N", "P", "Z") or ch in " -'":
            out.append(ch)
    return " ".join("".join(out).split()).strip()


def clean_existing_names(apps, schema_editor):
    Customer = apps.get_model("inventory", "Customer")
    Order = apps.get_model("inventory", "Order")

    to_update = []
    for c in Customer.objects.exclude(name="").only("id", "name").iterator():
        cleaned = _clean(c.name)
        if cleaned and cleaned != c.name:
            c.name = cleaned
            to_update.append(c)
        if len(to_update) >= 500:
            Customer.objects.bulk_update(to_update, ["name"])
            to_update = []
    if to_update:
        Customer.objects.bulk_update(to_update, ["name"])

    to_update = []
    for o in Order.objects.exclude(customer_name="").only("id", "customer_name").iterator():
        cleaned = _clean(o.customer_name)
        if cleaned and cleaned != o.customer_name:
            o.customer_name = cleaned
            to_update.append(o)
        if len(to_update) >= 500:
            Order.objects.bulk_update(to_update, ["customer_name"])
            to_update = []
    if to_update:
        Order.objects.bulk_update(to_update, ["customer_name"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0074_unifunl_orders_to_barats"),
    ]

    operations = [
        migrations.RunPython(clean_existing_names, noop_reverse),
    ]
