from django.apps import AppConfig


class ConcertsConfig(AppConfig):
    name = 'concerts'

    def ready(self):
        from . import signals  # noqa: F401  (connects the page regeneration)
