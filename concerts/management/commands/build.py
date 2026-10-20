from django.core.management.base import BaseCommand

from concerts import public


class Command(BaseCommand):
    help = "Regenerate every public band page (run daily so past dates drop off)."

    def handle(self, *args, **options):
        public.build_all()
