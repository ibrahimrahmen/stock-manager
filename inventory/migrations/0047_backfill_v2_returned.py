from django.db import migrations


def backfill_v2_returned(apps, schema_editor):
    """Existing v1 ShippingOrders already in RETURNED / PARTIAL_RETURNED whose
    linked v2 Order is not yet RETURNED → set the v2 Order to RETURNED.
    This catches returns scanned before the v1->v2 propagation existed.
    """
    ShippingOrder = apps.get_model("inventory", "ShippingOrder")
    Order = apps.get_model("inventory", "Order")

    RETURNED = "returned"
    so_returned = ("returned", "partial_returned")

    qs = (ShippingOrder.objects
          .filter(status__in=so_returned, order__isnull=False)
          .exclude(order__status=RETURNED)
          .select_related("order"))
    for so in qs:
        v2 = so.order
        if v2 and v2.status != RETURNED:
            v2.status = RETURNED
            v2.save(update_fields=["status"])


def noop_reverse(apps, schema_editor):
    # No safe automatic reverse (we can't know the prior status). Leave as-is.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0046_order_amount_collected"),
    ]

    operations = [
        migrations.RunPython(backfill_v2_returned, noop_reverse),
    ]
