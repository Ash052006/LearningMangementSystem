"""JSON endpoints under ``/api/v1/``.

Every rule comes from the shared services and access helpers, so the API is a
thin HTTP skin over the same logic the web views use: published courses only
for listings, the shared visibility rule for a course (drafts stay a 404 for
anyone who is neither the owner nor enrolled), an enrolment for learner
actions (403), and lesson order checked before a completion is recorded.
"""

from django.db.models import Count
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from courses.access import ensure_course_visible
from courses.models import Course, Enrollment
from courses.services import (
    can_access_lesson,
    course_progress,
    enrol,
    enrollments_with_progress,
    first_blocking_lesson,
    mark_lesson_complete,
    published_courses,
)

from .serializers import (
    CourseDetailSerializer,
    CourseListSerializer,
    EnrolmentSerializer,
    LessonCompletionSerializer,
)


class LessonLocked(APIException):
    """The lesson exists but an earlier one is not finished (HTTP 409).

    409 rather than 403 separates "you may not do this" (no enrolment) from
    "not yet": the learner is allowed here, the course order is not satisfied.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "This lesson is locked until the earlier lessons are complete."
    default_code = "lesson_locked"


class CourseListView(generics.ListAPIView):
    """Published courses, newest first, paginated.

    The base queryset is ``published_courses()`` (the same one the catalogue
    uses), so a draft can never appear here.
    """

    serializer_class = CourseListSerializer

    def get_queryset(self):
        return (
            published_courses()
            .select_related("instructor")
            .annotate(lesson_count=Count("lessons"))
            .order_by("-created_at", "-pk")
        )


class CourseDetailView(generics.RetrieveAPIView):
    """One course, if the requester may see it.

    Uses ``ensure_course_visible`` -- the identical rule the learner pages use
    -- so an unpublished course is a 404 for everyone except its owner or an
    already-enrolled learner.
    """

    serializer_class = CourseDetailSerializer

    def get_object(self) -> Course:
        queryset = Course.objects.select_related("instructor").prefetch_related(
            "lessons"
        )
        course = get_object_or_404(queryset, pk=self.kwargs["pk"])
        return ensure_course_visible(self.request.user, course)


class EnrolView(APIView):
    """POST: enrol the current user in a published course (idempotent).

    Delegates to the ``enrol`` service, so re-posting returns the same row; the
    response is 201 the first time and 200 afterwards.
    """

    @extend_schema(
        request=None,
        responses={201: EnrolmentSerializer, 200: EnrolmentSerializer},
        description="Enrol in a published course. Idempotent: 201 when a new "
        "enrolment is created, 200 when one already existed. A draft is a 404.",
    )
    def post(self, request, pk: int) -> Response:
        course = get_object_or_404(published_courses(), pk=pk)
        existed = Enrollment.objects.filter(user=request.user, course=course).exists()
        enrollment = enrol(request.user, course)
        serializer = EnrolmentSerializer(enrollment)
        code = status.HTTP_200_OK if existed else status.HTTP_201_CREATED
        return Response(serializer.data, status=code)


class MyEnrolmentsView(generics.ListAPIView):
    """The current user's enrolments, newest first, with progress."""

    serializer_class = EnrolmentSerializer

    def get_queryset(self):
        return enrollments_with_progress(self.request.user)


class LessonCompleteView(APIView):
    """POST: mark one lesson complete for the enrolled learner.

    Mirrors ``courses.views.LessonCompleteView``: the same ``can_access_lesson``
    gate is re-checked before writing, so a learner cannot skip ahead by
    calling the API directly.
    """

    @extend_schema(
        request=None,
        responses={201: LessonCompletionSerializer, 200: LessonCompletionSerializer},
        description="Mark a lesson complete. 403 without an enrolment; 409 when "
        "an earlier lesson still blocks it; 200 if it was already complete.",
    )
    def post(self, request, pk: int, lesson_pk: int) -> Response:
        course = get_object_or_404(Course, pk=pk)
        # A hidden course (draft, and the user is neither owner nor enrolled)
        # is a 404 before anything else.
        course = ensure_course_visible(request.user, course)
        enrollment = Enrollment.objects.filter(user=request.user, course=course).first()
        if enrollment is None:
            raise PermissionDenied("You are not enrolled in this course.")
        lesson = get_object_or_404(course.lessons, pk=lesson_pk)
        if not can_access_lesson(enrollment, lesson):
            blocking = first_blocking_lesson(enrollment, lesson)
            detail = LessonLocked.default_detail
            if blocking is not None:
                detail = (
                    f'Complete "{blocking.title}" first, then this lesson will unlock.'
                )
            raise LessonLocked(detail)
        created = mark_lesson_complete(enrollment, lesson)
        serializer = LessonCompletionSerializer(
            {
                "lesson": lesson,
                "created": created,
                "progress": course_progress(enrollment),
            }
        )
        code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(serializer.data, status=code)
