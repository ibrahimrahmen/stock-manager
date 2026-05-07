"""Middleware that enforces role-based URL access.

Currently only Messages Team is restricted (Shipping and Office are
intentionally overlapping — both can do everything except Messages-only stuff).

Superusers always bypass.
"""
from django.shortcuts import redirect


# URL prefixes Messages Team users are allowed to access.
# Anything else → redirected to the bubble page with a flash msg.
MESSAGES_TEAM_ALLOWED_PREFIXES = (
    "/",                  # bubble home itself (handled by exact-match below)
    "/login/",
    "/logout/",
    "/products/",         # mes produits + product detail
    "/search/",
    "/api/search/",       # search uses an API endpoint too
    "/static/",
    "/media/",
    "/favicon.ico",
)


def _is_allowed(path):
    """Return True if a Messages Team user may visit this path."""
    if path == "/":
        return True
    for prefix in MESSAGES_TEAM_ALLOWED_PREFIXES:
        if prefix == "/":
            continue
        if path.startswith(prefix):
            return True
    return False


class RoleAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)

        # Anonymous, superuser, or pre-auth requests — pass through.
        if not user or not user.is_authenticated or user.is_superuser:
            return self.get_response(request)

        # Get role; default to "office" (most permissive non-admin role).
        try:
            role = user.profile.role
        except Exception:
            role = "office"

        if role == "messages":
            if not _is_allowed(request.path):
                return redirect("home")

        return self.get_response(request)
