from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inventory"

    def ready(self):
        # Import signal handlers so they get registered.
        # Imported here (inside ready) to avoid circular imports at module load.
        from . import signals  # noqa: F401
