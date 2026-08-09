# Manually trimmed: add only Order.is_angry.
#
# `makemigrations` also wanted to drop several unrelated Ad / AdOfferLink /
# MessengerConversation fields (pre-existing model-vs-migration drift). Those
# are intentionally NOT applied here — dropping production columns/tables is out
# of scope for this feature and would be destructive. This migration adds the
# is_angry flag only; the drift stays exactly as it was before.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0071_order_sms_en_cours_tracking'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='is_angry',
            field=models.BooleanField(
                db_index=True, default=False,
                help_text='Auto: la conversation contient des mots de colère / insultes.',
            ),
        ),
    ]
