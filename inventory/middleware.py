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


# URL prefixes a Producteur may access — Testing & Production only.
PRODUCER_ALLOWED_PREFIXES = (
    "/testing-production/",   # entry, tabs, add, finish, per-test orders
    "/force-password/",
    "/logout/",
    "/static/",
    "/media/",
    "/favicon.ico",
)


def _producer_allowed(path):
    """Return True if a Producteur may visit this path."""
    for prefix in PRODUCER_ALLOWED_PREFIXES:
        if path.startswith(prefix):
            return True
    # Allow only the 'send an order back to the list' action from a test page.
    if path.startswith("/api/orders/") and path.endswith("/back-from-test/"):
        return True
    return False


class RoleAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)

        # Anonymous / pre-auth requests — pass through.
        if not user or not user.is_authenticated:
            return self.get_response(request)

        # Force temp-password change: a user flagged must_change_password is
        # redirected to the change-password page until they set a new one.
        # (Logout, the change page itself, and static/media are always allowed.)
        try:
            must_change = user.profile.must_change_password
        except Exception:
            must_change = False
        if must_change:
            _ok = ("/force-password/", "/logout/", "/static/", "/media/", "/favicon.ico")
            if not any(request.path.startswith(p) for p in _ok):
                return redirect("force_password_change")

        # Superuser — pass through (after the temp-password gate above).
        if user.is_superuser:
            return self.get_response(request)

        # Get role; default to "office" (most permissive non-admin role).
        try:
            role = user.profile.role
        except Exception:
            role = "office"

        if role == "messages":
            if not _is_allowed(request.path):
                return redirect("home")

        # Producteur: can ONLY see Testing & Production.
        if role == "producer":
            if not _producer_allowed(request.path):
                return redirect("testing_production_page")

        return self.get_response(request)
