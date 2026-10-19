"""Models for the courses app: courses, lessons, enrolments and progress.

Progress is never stored as a percentage; it is always derived from
LessonProgress rows (completed lessons / total lessons) by services.
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from .validators import validate_attachment_extension, validate_attachment_size


class Course(models.Model):
    """A training course created by an instructor."""

    class Status(models.TextChoices):
        """Publication state.  Only published courses are learner-visible."""

        DRAFT = "draft", _("Draft")
        PUBLISHED = "published", _("Published")

    # Deliberately NOT constrained to instructors at the DB level: user
    # history must survive role changes.  Model forms limit the queryset to
    # instructors instead (see forms in a later task).
    instructor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="courses",
        verbose_name=_("instructor"),
    )
    title = models.CharField(_("title"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    # When True, learners must complete lessons in order.  The rule is
    # enforced by services/views, not by the database.
    enforce_order = models.BooleanField(_("enforce order"), default=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("course")
        verbose_name_plural = _("courses")

    def __str__(self) -> str:
        return self.title

    @property
    def is_published(self) -> bool:
        """True when the course is visible to learners."""
        return self.status == self.Status.PUBLISHED

    @property
    def publishable(self) -> bool:
        """True when the course has at least one lesson (ready to publish)."""
        return self.lessons.exists()


class Lesson(models.Model):
    """A single ordered step inside a course."""

    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="lessons",
        verbose_name=_("course"),
    )
    title = models.CharField(_("title"), max_length=200)
    content = models.TextField(_("content"))
    video_url = models.URLField(_("video URL"), blank=True)
    attachment = models.FileField(
        _("attachment"),
        blank=True,
        upload_to="course_attachments/%Y/%m/",
        validators=[validate_attachment_size, validate_attachment_extension],
        help_text=_(
            "Optional file, maximum 10 MB: PDF, Office document, image or zip."
        ),
    )
    # Optional in forms; save() fills in the next free position for the
    # course when it is omitted (the column itself is NOT NULL).
    position = models.PositiveIntegerField(_("position"), blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "position"],
                name="unique_lesson_course_position",
            ),
        ]
        verbose_name = _("lesson")
        verbose_name_plural = _("lessons")

    def __str__(self) -> str:
        return f"{self.position}. {self.title}"

    def save(self, *args: object, **kwargs: object) -> None:
        """Auto-assign the next position when creating a lesson without one."""
        if self._state.adding and self.position is None:
            self.position = self._next_position()
        super().save(*args, **kwargs)

    def _next_position(self) -> int:
        """Return one past the highest existing position in this course."""
        highest = self.course.lessons.aggregate(highest=models.Max("position"))[
            "highest"
        ]
        return (highest or 0) + 1


class Enrollment(models.Model):
    """A learner's membership of a course."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="enrollments",
        verbose_name=_("user"),
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="enrollments",
        verbose_name=_("course"),
    )
    enrolled_at = models.DateTimeField(_("enrolled at"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "course"],
                name="unique_enrollment_user_course",
            ),
        ]
        verbose_name = _("enrollment")
        verbose_name_plural = _("enrollments")

    def __str__(self) -> str:
        return f"{self.user} in {self.course}"


class LessonProgress(models.Model):
    """Records that one lesson of an enrolment has been completed."""

    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.CASCADE,
        related_name="lesson_progress",
        verbose_name=_("enrollment"),
    )
    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name="completions",
        verbose_name=_("lesson"),
    )
    completed_at = models.DateTimeField(_("completed at"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["enrollment", "lesson"],
                name="unique_lesson_progress",
            ),
        ]
        verbose_name = _("lesson progress")
        verbose_name_plural = _("lesson progress records")

    def __str__(self) -> str:
        return f"{self.lesson} completed by {self.enrollment.user}"

    def clean(self) -> None:
        """The lesson must belong to the same course as the enrolment."""
        super().clean()
        if self.enrollment_id and self.lesson_id:
            if self.lesson.course_id != self.enrollment.course_id:
                raise ValidationError(
                    {"lesson": _("Lesson does not belong to this enrolment's course.")}
                )


class Certificate(models.Model):
    """A completion certificate for one enrolment, earned once and kept.

    It is issued only when the enrolment meets every requirement at that
    moment (all lessons complete, quiz passed or absent — see
    ``services.issue_certificate_if_eligible``).  Because the row is a snapshot,
    a certificate stays valid afterwards: adding a lesson to the course later
    does not revoke it.  The random ``code`` is the public handle used by the
    verification page, and the certificate is tied to the enrolment (so
    changing the learner's display name updates what is printed).
    """

    enrollment = models.OneToOneField(
        Enrollment,
        on_delete=models.CASCADE,
        related_name="certificate",
        verbose_name=_("enrollment"),
    )
    # 122 bits of randomness, generated on issue.  ``editable=False``: the code
    # is never user-supplied, and ``unique=True`` makes it a public lookup key.
    code = models.UUIDField(_("code"), default=uuid.uuid4, unique=True, editable=False)
    issued_at = models.DateTimeField(_("issued at"), auto_now_add=True)

    class Meta:
        ordering = ["-issued_at"]
        verbose_name = _("certificate")
        verbose_name_plural = _("certificates")

    def __str__(self) -> str:
        return f"{self.enrollment} — {self.code}"

    @property
    def course(self) -> Course:
        """The course the certificate was earned for."""
        return self.enrollment.course

    @property
    def holder_name(self) -> str:
        """The name printed on the certificate.

        Falls back to the username when the learner has no full name on file,
        so a certificate is never blank.
        """
        return (
            self.enrollment.user.get_full_name() or self.enrollment.user.get_username()
        )
