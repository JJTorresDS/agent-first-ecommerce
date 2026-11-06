import json

from django.core.management.base import BaseCommand

from support.models import ErrorReport


class Command(BaseCommand):
    help = "Print error reports as JSON, newest first. Read-only: for whoever debugs them."

    def add_arguments(self, parser):
        parser.add_argument("--open", action="store_true", help="Only reports not resolved yet.")
        parser.add_argument("--id", type=int, help="One report.")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        rows = ErrorReport.objects.select_related("reported_by").order_by("-updated_at")
        if options["id"]:
            rows = rows.filter(pk=options["id"])
        if options["open"]:
            rows = rows.filter(status=ErrorReport.Status.OPEN)
        reports = [row.as_dict() for row in rows[: max(1, options["limit"])]]
        self.stdout.write(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
