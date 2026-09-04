"""Tests for the append-only audit log and the system-settings singleton."""

from django.test import TestCase

from apps.accounts.models import Role, User
from apps.core.choices import AuditAction
from apps.core.models import AppendOnlyError, AuditLog, SystemSettings
from apps.core.services import record_audit, snapshot


class AuditLogTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="auditor@maxifyfx.com",
            password="portal-test-pass-12345",
            full_name="Auditor",
            role=Role.FINANCE_ADMIN,
        )

    def test_entry_cannot_be_modified(self):
        entry = record_audit(
            action=AuditAction.STATUS_CHANGE, target=self.user, actor=self.user, after={"x": 1}
        )
        entry.action = AuditAction.RATE_CHANGE
        with self.assertRaises(AppendOnlyError):
            entry.save()

    def test_entry_cannot_be_deleted(self):
        entry = record_audit(action=AuditAction.LOGIN, target=self.user, actor=self.user)
        with self.assertRaises(AppendOnlyError):
            entry.delete()

    def test_record_audit_captures_actor_label_and_target(self):
        entry = record_audit(
            action=AuditAction.PERMISSION_CHANGE,
            target=self.user,
            actor=self.user,
            before={"groups": []},
            after={"groups": ["finance_admin"]},
        )
        self.assertEqual(entry.target_type, "accounts.User")
        self.assertEqual(entry.target_id, str(self.user.pk))
        self.assertIn("Auditor", entry.actor_label)
        self.assertEqual(entry.after, {"groups": ["finance_admin"]})

    def test_actor_label_outlives_a_deactivated_actor(self):
        entry = record_audit(action=AuditAction.LOGIN, target=self.user, actor=self.user)
        self.user.is_active = False
        self.user.save()
        self.assertTrue(AuditLog.objects.get(pk=entry.pk).actor_label)

    def test_snapshot_is_json_safe(self):
        data = snapshot(self.user, ["email", "role", "is_active", "date_joined"])
        self.assertEqual(data["email"], "auditor@maxifyfx.com")
        self.assertIsInstance(data["date_joined"], str)

    def test_record_audit_needs_a_target(self):
        with self.assertRaises(ValueError):
            record_audit(action=AuditAction.LOGIN)


class SystemSettingsTests(TestCase):
    def test_load_creates_and_reuses_one_row(self):
        first = SystemSettings.load()
        second = SystemSettings.load()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SystemSettings.objects.count(), 1)

    def test_saving_a_second_instance_overwrites_the_singleton(self):
        SystemSettings.load()
        SystemSettings(closed_message_ar="مغلق").save()
        self.assertEqual(SystemSettings.objects.count(), 1)
        self.assertEqual(SystemSettings.load().closed_message_ar, "مغلق")

    def test_cannot_be_deleted(self):
        with self.assertRaises(AppendOnlyError):
            SystemSettings.load().delete()

    def test_invalid_timezone_is_rejected(self):
        from django.core.exceptions import ValidationError

        settings_row = SystemSettings.load()
        settings_row.timezone = "Not/AZone"
        with self.assertRaises(ValidationError):
            settings_row.full_clean()
