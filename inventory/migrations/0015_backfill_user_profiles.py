# Data migration: ensure every existing User has a UserProfile.
# Safe — only INSERTs missing profile rows. Never updates existing ones.

from django.db import migrations


def create_missing_profiles(apps, schema_editor):
    User = apps.get_model("auth", "User")
    UserProfile = apps.get_model("inventory", "UserProfile")
    for user in User.objects.all():
        UserProfile.objects.get_or_create(user_id=user.id)


def reverse_noop(apps, schema_editor):
    # Reversal does nothing — we don't want to delete profiles on rollback.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0014_userprofile'),
    ]

    operations = [
        migrations.RunPython(create_missing_profiles, reverse_noop),
    ]
