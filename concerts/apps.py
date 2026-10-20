from django.apps import AppConfig


class ConcertsConfig(AppConfig):
    name = "concerts"

    def ready(self) -> None:
        from . import signals  # noqa: F401  (connects the page regeneration)
