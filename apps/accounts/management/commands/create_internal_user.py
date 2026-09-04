"""Create an internal account from the command line.

Needed to bootstrap the very first ``finance_admin``; after that, accounts are
created from the admin by a ``finance_admin`` (spec §3).
"""

import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Role, User
from apps.accounts.permissions import sync_role_groups
from apps.core.choices import AuditAction
from apps.core.services import record_audit, snapshot


class Command(BaseCommand):
    help = "Create an internal user (finance_admin, finance_staff or merchant)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--full-name", required=True)
        parser.add_argument("--role", required=True, choices=Role.values)
        parser.add_argument(
            "--password",
            help="Omit to be prompted. Passing it on the command line leaves it in shell history.",
        )
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Also grant is_superuser. Only valid with --role finance_admin.",
        )

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        role = options["role"]

        if options["superuser"] and role != Role.FINANCE_ADMIN:
            raise CommandError("--superuser is only valid with --role finance_admin.")
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(f"An account already exists for {email}.")

        password = options.get("password")
        if not password:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                raise CommandError("Passwords did not match.")

        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

        # Groups must exist before the post_save signal tries to attach one.
        sync_role_groups(strict=False)

        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=options["full_name"],
                role=role,
                is_superuser=options["superuser"],
            )
            record_audit(
                action=AuditAction.USER_CHANGE,
                target=user,
                actor=user,
                actor_label="manage.py create_internal_user",
                after=snapshot(user, ["email", "full_name", "role", "is_active", "is_superuser"]),
            )

        self.stdout.write(self.style.SUCCESS(f"Created {role} account: {email}"))
        self.stdout.write(
            "Two-factor enrolment is mandatory and happens on first login at "
            "/account/two_factor/setup/."
        )
