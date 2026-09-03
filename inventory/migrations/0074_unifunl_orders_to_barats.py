"""Re-tag already-imported Unifunl orders to the Barats sales page.

The first version of the Unifunl sync filed orders under a separate "Unifunl"
sales page. Unifunl actually handles the Barats social DMs, so those orders
belong to the normal "Barats" page like every other Barats order. This moves
any order sitting on the "Unifunl" page over to "Barats" and removes the now
empty "Unifunl" page. No order is deleted.
"""
from django.db import migrations


def move_unifunl_orders_to_barats(apps, schema_editor):
    SalesPage = apps.get_model("inventory", "SalesPage")
    Order = apps.get_model("inventory", "Order")

    uni = SalesPage.objects.filter(name__iexact="Unifunl").first()
    if not uni:
        return
    barats = (SalesPage.objects.filter(pk=3).first()
              or SalesPage.objects.filter(name__iexact="Barats").first()
              or SalesPage.objects.filter(name__iexact="Barats.tn").first())
    if not barats:
        # No Barats page to move to — leave things as-is rather than lose the tag.
        return
    Order.objects.filter(sales_page=uni).update(sales_page=barats)
    # The Unifunl page is now empty; drop it. Guarded so any unexpected
    # reference (which would raise) leaves the page in place, deactivated.
    try:
        uni.delete()
    except Exception:
        uni.is_active = False
        uni.save(update_fields=["is_active"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0073_alter_adofferlink_id_and_more"),
    ]

    operations = [
        migrations.RunPython(move_unifunl_orders_to_barats, noop_reverse),
    ]
