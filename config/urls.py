from django.conf import settings
from django.contrib import admin
from django.http import FileResponse, HttpRequest, HttpResponse
from django.urls import URLPattern, URLResolver, path
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.static import serve

urlpatterns: list[URLPattern | URLResolver] = [
    path("admin/", admin.site.urls),
]


@xframe_options_exempt  # the widget must be allowed in other sites' iframes
def public_file(request: HttpRequest, path: str) -> HttpResponse | FileResponse:
    """Development only: serve the generated public files, so runserver alone is enough to try them."""
    if path == "" or path.endswith("/"):
        path += "index.html"
    response = serve(request, path, document_root=str(settings.PUBLIC_DIR))
    if path.endswith("events.json"):
        # Same rule as the production server (step 5): band websites may read this public file
        response["Access-Control-Allow-Origin"] = "*"
    return response


if settings.DEBUG:
    urlpatterns.append(path("<path:path>", public_file))
