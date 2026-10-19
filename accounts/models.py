"""Custom user model for the staff-training site.

This must be referenced by AUTH_USER_MODEL before the first migration is
created, so the whole project (including later apps) resolves foreign keys to
``accounts.User``.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """Staff member who is either an instructor or a learner."""

    class Role(models.TextChoices):
        """Role options for the LMS.  Stored as plain strings."""

        INSTRUCTOR = "instructor", _("Instructor")
        LEARNER = "learner", _("Learner")

    role = models.CharField(
        _("role"),
        max_length=20,
        choices=Role.choices,
        default=Role.LEARNER,
    )

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")

    def __str__(self) -> str:
        return self.get_username()

    @property
    def is_instructor(self) -> bool:
        """True when the user may create and manage courses."""
        return self.role == self.Role.INSTRUCTOR

    @property
    def is_learner(self) -> bool:
        """True when the user may enrol in courses and take quizzes."""
        return self.role == self.Role.LEARNER
