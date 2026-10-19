"""Serializers for the JSON API.

The API exposes the same models the web pages do; nothing here re-implements a
rule.  In particular, progress is derived through the shared service helpers
(``course_progress`` / ``progress_percent``), never stored or recomputed with a
second formula, so the API and the web pages always agree.
"""

from rest_framework import serializers

from courses.models import Course, Enrollment, Lesson
from courses.services import course_progress, progress_percent


class LessonSerializer(serializers.ModelSerializer):
    """A lesson's identity and position (its body is not needed to track it)."""

    class Meta:
        model = Lesson
        fields = ["id", "position", "title"]


class CourseSummarySerializer(serializers.ModelSerializer):
    """A light course reference for nesting inside another payload."""

    instructor = serializers.CharField(source="instructor.username", read_only=True)

    class Meta:
        model = Course
        fields = ["id", "title", "instructor"]


class CourseListSerializer(serializers.ModelSerializer):
    """One row of the published-course list.  ``lesson_count`` is annotated."""

    instructor = serializers.CharField(source="instructor.username", read_only=True)
    lesson_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Course
        fields = ["id", "title", "instructor", "lesson_count", "created_at"]


class CourseDetailSerializer(serializers.ModelSerializer):
    """Full course detail, including the ordered lesson list."""

    instructor = serializers.CharField(source="instructor.username", read_only=True)
    lesson_count = serializers.SerializerMethodField()
    lessons = LessonSerializer(many=True, read_only=True)

    class Meta:
        model = Course
        fields = [
            "id",
            "title",
            "description",
            "instructor",
            "status",
            "enforce_order",
            "lesson_count",
            "lessons",
            "created_at",
            "updated_at",
        ]

    def get_lesson_count(self, obj: Course) -> int:
        # Reuse the prefetched relation rather than issuing a COUNT query.
        return len(obj.lessons.all())


class EnrolmentSerializer(serializers.ModelSerializer):
    """An enrolment with derived progress.

    ``MyCoursesView`` annotates the same counts (``completed_lessons`` /
    ``total_lessons``); when the caller did not annotate, this falls back to the
    shared ``course_progress`` helper so the figures are never re-invented.
    """

    course = CourseSummarySerializer(read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = Enrollment
        fields = ["id", "course", "enrolled_at", "progress"]

    def get_progress(self, obj: Enrollment) -> dict[str, int]:
        completed = getattr(obj, "completed_lessons", None)
        total = getattr(obj, "total_lessons", None)
        if completed is None or total is None:
            return course_progress(obj)
        return {
            "completed": completed,
            "total": total,
            "percent": progress_percent(completed, total),
        }


class LessonCompletionSerializer(serializers.Serializer):
    """Result of marking a lesson complete."""

    lesson = LessonSerializer()
    created = serializers.BooleanField()
    progress = serializers.DictField(child=serializers.IntegerField())
