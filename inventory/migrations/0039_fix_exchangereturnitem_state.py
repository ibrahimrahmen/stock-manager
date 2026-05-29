from django.db import migrations, models


class Migration(migrations.Migration):
    """Update Django's internal migration state for ExchangeReturnItem's index
    name without actually altering the DB.

    Why this is needed:
    - Django generated an auto-migration that wanted to rename
      `inventory_e_exchang_a7e8c6_idx` → `inventory_e_exchang_04b9e9_idx`
    - But the original index name doesn't exist in the production DB
      (the model was created when the index was generated with the *new* name
      directly, so the rename failed with "relation does not exist").
    - Using `SeparateDatabaseAndState` lets us reconcile Django's view of the
      schema with reality, without running any SQL.

    Safe: no DB statements are executed.
    Risk: zero.
    """

    dependencies = [
        ("inventory", "0038_productunit_early_return"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # DB side: do nothing
            database_operations=[],
            # State side: update Django's view to match what's actually in DB
            state_operations=[
                migrations.RenameIndex(
                    model_name="exchangereturnitem",
                    new_name="inventory_e_exchang_04b9e9_idx",
                    old_name="inventory_e_exchang_a7e8c6_idx",
                ),
                migrations.AlterField(
                    model_name="exchangereturnitem",
                    name="id",
                    field=models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID",
                    ),
                ),
            ],
        ),
    ]
