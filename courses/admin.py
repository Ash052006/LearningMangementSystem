"""Admin registration for courses: courses, lessons, enrolments, progress.

Querysets use select_related/annotate so each changelist stays constant-query
rather than one query per row.
"""

from django.contrib import admin
from django.db.models import Count, QuerySet
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from .models import Certificate, Course, Enrollment, Lesson, LessonProgress


class LessonInline(admin.TabularInline):
    """Lessons shown on the course page, ordered by their position."""

    model = Lesson
    extra = 0
    fields = ["position", "title", "video_url", "attachment"]
    ordering = ["position"]
    # Needs Lesson to be registered on its own too (it is, below).
    show_change_link = True


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    """Courses with lesson/enrolment counts and an ordered lesson inline."""

    list_display = [
        "title",
        "instructor",
        "status",
        "lesson_count",
        "enrollment_count",
    ]
    list_filter = ["status", "instructor"]
    search_fields = [
        "title",
        "description",
        "instructor__username",
        "instructor__email",
    ]
    inlines = [LessonInline]

    def get_queryset(self, request: HttpRequest) -> QuerySet[Course]:
        """Join the instructor and pre-count lessons/enrolments."""
        return (
            super()
            .get_queryset(request)
            .select_related("instructor")
            .annotate(
                lesson_total=Count("lessons", distinct=True),
                enrollment_total=Count("enrollments", distinct=True),
            )
        )

    @admin.display(description=_("lessons"), ordering="lesson_total")
    def lesson_count(self, obj: Course) -> int:
        """Number of lessons in the course."""
        return obj.lesson_total

    @admin.display(description=_("enrolments"), ordering="enrollment_total")
    def enrollment_count(self, obj: Course) -> int:
        """Number of learners enrolled in the course."""
        return obj.enrollment_total


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    """Lessons standalone; also linked from the course inline."""

    list_display = ["position", "title", "course"]
    list_filter = ["course"]
    search_fields = ["title", "content", "course__title"]
    list_select_related = ["course"]
    ordering = ["course", "position"]


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    """Enrolments, filterable by course and browsable by date."""

    list_display = ["user", "course", "enrolled_at"]
    list_filter = ["course"]
    search_fields = ["user__username", "course__title"]
    date_hierarchy = "enrolled_at"
    list_select_related = ["user", "course"]


@admin.register(LessonProgress)
class LessonProgressAdmin(admin.ModelAdmin):
    """Completed-lesson records, filterable by the lesson's course."""

    list_display = ["enrollment", "lesson", "completed_at"]
    list_filter = ["lesson__course"]
    search_fields = ["enrollment__user__username", "lesson__title"]
    date_hierarchy = "completed_at"
    list_select_related = ["enrollment__user", "enrollment__course", "lesson"]


@admin.register(Certificate)
class CertificateAdmin(admin.ModelAdmin):
    """Issued certificates.  Read-only: rows are created by the learner flow."""

    list_display = ["code", "enrollment", "issued_at"]
    list_filter = ["enrollment__course"]
    search_fields = [
        "code",
        "enrollment__user__username",
        "enrollment__course__title",
    ]
    date_hierarchy = "issued_at"
    list_select_related = ["enrollment__user", "enrollment__course"]
    readonly_fields = ["enrollment", "code", "issued_at"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Certificates are issued by the app, never typed into the admin."""
        return False
