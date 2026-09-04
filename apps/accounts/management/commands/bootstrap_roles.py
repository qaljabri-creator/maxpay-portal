"""Create the role groups and reset each to its baseline permission set.

Idempotent — safe to run on every deploy, and run automatically after
``migrate`` via the ``post_migrate`` hook.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.permissions import (
    MERCHANT_FORBIDDEN,
    PermissionMatrixError,
    expected_permissions,
    sync_role_groups,
)


class Command(BaseCommand):
    help = "Sync the finance_admin / finance_staff / merchant groups with the permission matrix."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what each role would receive without writing anything.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            for role in ("finance_admin", "finance_staff", "merchant"):
                labels = sorted(expected_permissions(role))
                self.stdout.write(self.style.MIGRATE_HEADING(f"{role} ({len(labels)})"))
                for label in labels:
                    self.stdout.write(f"  {label}")
            return

        try:
            results = sync_role_groups(strict=True)
        except PermissionMatrixError as exc:
            raise CommandError(str(exc)) from exc

        for role, count in sorted(results.items()):
            self.stdout.write(self.style.SUCCESS(f"{role}: {count} permissions"))

        merchant_labels = expected_permissions("merchant")
        self.stdout.write(
            self.style.SUCCESS(
                "Client anonymity check passed: merchant holds none of the "
                f"{len(MERCHANT_FORBIDDEN)} identity permissions."
            )
        )
        assert not (merchant_labels & MERCHANT_FORBIDDEN)
