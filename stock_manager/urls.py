from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.http import FileResponse, Http404
from pathlib import Path


def _serve_media(request, path):
    """Stream a media file from MEDIA_ROOT.

    django.views.static.serve loads files into memory, which kills Gunicorn
    workers (OOM) when serving many product images. FileResponse uses the
    kernel's sendfile() — zero-copy streaming — instead.
    Also blocks any path-traversal attempt via '..'.
    """
    media_root = Path(settings.MEDIA_ROOT).resolve()
    file_path = (media_root / path).resolve()
    # Security: refuse if the resolved path escapes MEDIA_ROOT
    if media_root not in file_path.parents and file_path != media_root:
        raise Http404()
    if not file_path.is_file():
        raise Http404()
    # FileResponse handles Content-Type, Last-Modified, ETag, range requests.
    response = FileResponse(open(file_path, "rb"))
    # Cache for 7 days — product images rarely change, this stops the browser
    # from re-fetching them on every page navigation.
    response["Cache-Control"] = "public, max-age=604800, immutable"
    return response


urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('inventory.urls')),
    re_path(r'^media/(?P<path>.*)$', _serve_media),
]
