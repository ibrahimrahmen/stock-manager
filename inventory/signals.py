"""Signal handlers for the inventory app.

Auto-records login and logout to the AuditLog. Each new piece of behaviour
should add its own handler here rather than scattering log_action() calls
through views (when possible).
"""
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.contrib.auth.models import User
from django.contrib.admin.models import LogEntry, ADDITION, CHANGE, DELETION
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AuditLog, UserProfile, log_action


@receiver(post_save, sender=User)
def _create_profile(sender, instance, created, **kwargs):
    """Every new user gets a Profile (default role = office)."""
    if created:
        UserProfile.objects.get_or_create(user=instance)
    else:
        # Make sure existing users always have a profile too — idempotent.
        UserProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=LogEntry)
def _mirror_admin_action(sender, instance, created, **kwargs):
    """Every action a user takes inside Django /admin/ is logged into LogEntry
    by Django itself. We mirror those here so they appear in our AuditLog
    alongside scan/edit actions, giving a single timeline."""
    if not created:
        return
    try:
        action_map = {
            ADDITION: AuditLog.CREATE,
            CHANGE:   AuditLog.EDIT,
            DELETION: AuditLog.DELETE,
        }
        action = action_map.get(instance.action_flag, AuditLog.OTHER)
        model_name = instance.content_type.model if instance.content_type else ""
        # change_message contains a human-readable summary like "Changed name."
        msg = instance.change_message or instance.object_repr
        AuditLog.objects.create(
            user=instance.user,
            username=instance.user.username if instance.user else "admin",
            action=action,
            description=f"[Admin] {model_name} '{instance.object_repr[:80]}' — {msg[:200]}",
            target_model=model_name[:80],
            target_id=str(instance.object_id or "")[:50],
        )
    except Exception:
        pass


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    log_action(
        user, AuditLog.LOGIN,
        description=f"Connexion de {user.username}",
        request=request,
    )


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    if user is None:
        return
    log_action(
        user, AuditLog.LOGOUT,
        description=f"Déconnexion de {user.username}",
        request=request,
    )


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request, **kwargs):
    # Record failed login attempts — useful for catching brute-force or typos.
    try:
        username = (credentials or {}).get("username", "")[:150]
        AuditLog.objects.create(
            user=None,
            username=username or "unknown",
            action=AuditLog.OTHER,
            description=f"Échec de connexion pour '{username}'",
            ip_address=(
                (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                 or request.META.get("REMOTE_ADDR")) if request else None
            ),
        )
    except Exception:
        pass
