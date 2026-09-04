from django.contrib.auth.base_user import BaseUserManager
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager):
    """Manager for the internal-user model.

    There is no self-registration for any internal role (spec §3) — accounts are
    created by a ``finance_admin`` through the admin, or by the
    ``create_internal_user`` management command when bootstrapping.
    """

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError(_("البريد الإلكتروني مطلوب."))
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            # No usable password: the account cannot be logged into until a
            # finance_admin sets one.
            user.set_unusable_password()
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("is_active", True)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        from .models import Role

        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("role", Role.FINANCE_ADMIN)

        if extra_fields.get("is_superuser") is not True:
            raise ValueError(_("يجب أن يكون المستخدم الأعلى is_superuser=True."))
        if extra_fields.get("role") != Role.FINANCE_ADMIN:
            raise ValueError(_("المستخدم الأعلى يجب أن يكون بدور مدير مالي."))
        return self._create_user(email, password, **extra_fields)

    def internal(self):
        return self.get_queryset()

    def finance(self):
        from .models import Role

        return self.get_queryset().filter(
            role__in=[Role.FINANCE_ADMIN, Role.FINANCE_STAFF]
        )

    def merchants(self):
        from .models import Role

        return self.get_queryset().filter(role=Role.MERCHANT)
