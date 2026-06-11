from django.db import migrations


def backfill_v2_payee(apps, schema_editor):
    """Existing v1 ShippingOrders already PAID (paid before the payee flip
    existed) whose linked v2 Order is still in a non-final state → set the v2
    Order to PAYEE. Never override RETURNED (returns stay final).
    """
    ShippingOrder = apps.get_model("inventory", "ShippingOrder")

    PAID = "paid"
    PAYEE = "payee"
    RETURNED = "returned"

    qs = (ShippingOrder.objects
          .filter(status=PAID, order__isnull=False)
          .exclude(order__status__in=(PAYEE, RETURNED))
          .select_related("order"))
    for so in qs:
        v2 = so.order
        if v2 and v2.status not in (PAYEE, RETURNED):
            v2.status = PAYEE
            # mirror collected amount if we have it and v2 doesn't
            if getattr(so, "amount_collected", None) is not None and v2.amount_collected is None:
                v2.amount_collected = so.amount_collected
                v2.save(update_fields=["status", "amount_collected"])
            else:
                v2.save(update_fields=["status"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0048_alter_order_status"),
    ]

    operations = [
        migrations.RunPython(backfill_v2_payee, noop_reverse),
    ]
