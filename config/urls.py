from django.conf import settings
from django.contrib import admin
from django.http import FileResponse, HttpRequest, HttpResponse
from django.urls import URLPattern, URLResolver, path
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.static import serve

from concerts import login

urlpatterns: list[URLPattern | URLResolver] = [
    path("admin/", admin.site.urls),
    # Before the public files, which take every other address (bands cannot use these: RESERVED_SLUGS)
    path("login/", login.ask_link, name="login"),
    path("login/<str:uid>/<str:token>/", login.use_link, name="login_link"),
]


@xframe_options_exempt  # the widget must be allowed in other sites' iframes
def public_file(request: HttpRequest, path: str) -> HttpResponse | FileResponse:
    """Serve the generated public files (see SERVE_FILES)."""
    if path == "" or path.endswith("/"):
        path += "index.html"
    response = serve(request, path, document_root=str(settings.PUBLIC_DIR))
    if path.endswith("events.json"):
        # Same rule as the production server (step 5): band websites may read this public file
        response["Access-Control-Allow-Origin"] = "*"
    return response


if settings.SERVE_FILES:
    # ponytail: Django serving files is fine for a demo; a web server (Apache, Caddy…) should do it at scale
    urlpatterns += [
        path("static/<path:path>", serve, {"document_root": str(settings.STATIC_ROOT)}),
        path("", public_file, {"path": ""}),  # "<path:path>" needs at least one character
        path("<path:path>", public_file),
    ]
