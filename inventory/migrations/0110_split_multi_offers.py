from django.db import migrations


def split_multi_offers(apps, schema_editor):
    """Normalise legacy orders to the Barats.tn / Shopify storage shape.

    Old storefront orders stored 'the same set ×N' as ONE OrderOffer with
    quantity=N. New orders store N separate OrderOffer rows (quantity=1 each).
    This splits every quantity>1 offer into N quantity-1 rows, copying its
    component lines, so the whole app (list, order page, '+ Ajouter des
    articles' editor) shows them consistently as N separate sets.

    Money is unchanged: offer_total was bundle_price*N on one row; it becomes
    bundle_price*1 on N rows — the same sum. The cached Order.total is not
    touched. Stock is not touched (already deducted at order time). Reverse is
    a no-op (the split cannot be cleanly undone, and need not be).
    """
    OrderOffer = apps.get_model("inventory", "OrderOffer")
    OrderLine = apps.get_model("inventory", "OrderLine")

    def per_copy(lq, old_q):
        # Component qty is stored per bundle, or already multiplied by the copy
        # count. Only divide when it is a clean multiple of the copy count.
        if old_q > 1 and lq >= old_q and lq % old_q == 0:
            return lq // old_q
        return lq

    for oo in list(OrderOffer.objects.filter(quantity__gt=1)):
        old_q = oo.quantity or 1
        if old_q <= 1:
            continue
        lines = list(OrderLine.objects.filter(order_offer=oo))
        # Create N-1 extra offer rows, each a full copy of this set.
        for _ in range(old_q - 1):
            new_oo = OrderOffer.objects.create(
                order_id=oo.order_id, offer_id=oo.offer_id,
                offer_name=oo.offer_name, bundle_price=oo.bundle_price,
                quantity=1)
            for l in lines:
                OrderLine.objects.create(
                    order_id=l.order_id, order_offer=new_oo,
                    product_id=l.product_id, variant_id=l.variant_id,
                    size=l.size, quantity=per_copy(l.quantity or 1, old_q),
                    unit_price=l.unit_price)
        # Normalise the original row to a single copy.
        for l in lines:
            nq = per_copy(l.quantity or 1, old_q)
            if l.quantity != nq:
                l.quantity = nq
                l.save(update_fields=["quantity"])
        oo.quantity = 1
        oo.save(update_fields=["quantity"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0109_storehit"),
    ]

    operations = [
        migrations.RunPython(split_multi_offers, noop),
    ]
