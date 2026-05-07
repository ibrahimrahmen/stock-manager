"""Signal handlers for the inventory app.

Auto-records login and logout to the AuditLog. Each new piece of behaviour
should add its own handler here rather than scattering log_action() calls
through views (when possible).
"""
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from .models import AuditLog, log_action


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
