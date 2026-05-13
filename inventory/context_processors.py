"""Template context processors for the inventory app."""


def user_theme(request):
    """Inject the logged-in user's theme preference into every template context.

    Resolves to 'dark' by default. Anonymous users always get 'dark'.
    The template uses {{ user_theme }} (e.g. <body data-theme="{{ user_theme }}">)
    to apply the right CSS variables.
    """
    try:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return {"user_theme": "dark"}
        profile = getattr(user, "profile", None)
        if profile is None:
            return {"user_theme": "dark"}
        return {"user_theme": profile.theme or "dark"}
    except Exception:
        return {"user_theme": "dark"}
