"""Tests for the JSON API (/api/v1/).

They exercise the same R7 access rules the web tests cover -- draft 404,
unenrolled 403, and instructor-only writes kept off the API -- through an
``APIClient``, plus auth, pagination, progress and throttling.
"""

from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from rest_framework.throttling import UserRateThrottle

from courses.models import Course, Enrollment, Lesson, LessonProgress
from courses.services import enrol, mark_lesson_complete

User = get_user_model()

OK = 200
CREATED = 201
UNAUTHORIZED = 401
FORBIDDEN = 403
NOT_FOUND = 404
METHOD_NOT_ALLOWED = 405
CONFLICT = 409
TOO_MANY_REQUESTS = 429


def make_instructor(username: str = "inst") -> User:
    """Create an instructor (no password: tests never authenticate by password)."""
    return User.objects.create_user(username=username, role=User.Role.INSTRUCTOR)


def make_learner(username: str = "learner") -> User:
    """Create a learner (no password, as above)."""
    return User.objects.create_user(username=username)


def make_course(instructor: User, title: str = "Law 101") -> Course:
    """Create a draft course owned by ``instructor``."""
    return Course.objects.create(instructor=instructor, title=title)


def make_lesson(course: Course, title: str = "Intro", **kwargs: object) -> Lesson:
    """Create a lesson inside ``course``, auto-assigning its position."""
    return Lesson.objects.create(course=course, title=title, content="...", **kwargs)


def publish(course: Course) -> Course:
    """Set a course to published."""
    course.status = Course.Status.PUBLISHED
    course.save()
    return course


class APITestCase(TestCase):
    """Shared fixtures: an instructor, a learner and a published course."""

    def setUp(self) -> None:
        # DRF throttles count in the cache, which persists across tests in one
        # process; clearing it keeps each test's request budget independent.
        cache.clear()
        self.instructor = make_instructor("teacher")
        self.learner = make_learner("student")
        self.course = publish(make_course(self.instructor, "Evidence"))
        self.course.description = "How to handle evidence."
        self.course.save()
        self.lesson = make_lesson(self.course, "First")
        self.second_lesson = make_lesson(self.course, "Second")
        self.client = APIClient()

    def login(self, user: User) -> None:
        self.client.force_login(user)


class AuthenticationTests(APITestCase):
    """Both session and token auth work; anonymous access is refused."""

    def test_anonymous_is_unauthorized(self) -> None:
        response = self.client.get(reverse("api:course-list"))
        self.assertEqual(response.status_code, UNAUTHORIZED)

    def test_session_auth_allows_access(self) -> None:
        self.login(self.learner)
        self.assertEqual(self.client.get(reverse("api:course-list")).status_code, OK)

    def test_token_auth_allows_access(self) -> None:
        token = Token.objects.create(user=self.learner)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(self.client.get(reverse("api:course-list")).status_code, OK)

    def test_token_endpoint_exchanges_credentials(self) -> None:
        self.learner.set_password("pw-12345!")
        self.learner.save()
        response = self.client.post(
            reverse("api:token"),
            {"username": "student", "password": "pw-12345!"},
            format="json",
        )
        self.assertEqual(response.status_code, OK)
        self.assertIn("token", response.data)


class CourseListAPITests(APITestCase):
    """The list is published-only, annotated and paginated."""

    def setUp(self) -> None:
        super().setUp()
        self.login(self.learner)

    def test_lists_published_courses_only(self) -> None:
        draft = make_course(self.instructor, "Draft course")
        response = self.client.get(reverse("api:course-list"))
        titles = [row["title"] for row in response.data["results"]]
        self.assertIn("Evidence", titles)
        self.assertNotIn(draft.title, titles)

    def test_lesson_count_is_included(self) -> None:
        response = self.client.get(reverse("api:course-list"))
        self.assertEqual(response.data["results"][0]["lesson_count"], 2)

    def test_results_are_paginated(self) -> None:
        for index in range(settings.REST_FRAMEWORK["PAGE_SIZE"] + 1):
            publish(make_course(self.instructor, f"Extra {index}"))
        response = self.client.get(reverse("api:course-list"))
        self.assertEqual(
            response.data["count"], settings.REST_FRAMEWORK["PAGE_SIZE"] + 2
        )
        self.assertEqual(
            len(response.data["results"]), settings.REST_FRAMEWORK["PAGE_SIZE"]
        )
        self.assertIsNotNone(response.data["next"])


class CourseDetailAPITests(APITestCase):
    """Draft 404 / owner-preview / enrolled-access, via the shared rule."""

    def detail(self, course: Course):
        return self.client.get(reverse("api:course-detail", args=[course.pk]))

    def test_published_course_is_visible_with_lessons(self) -> None:
        self.login(self.learner)
        response = self.detail(self.course)
        self.assertEqual(response.status_code, OK)
        self.assertEqual(response.data["title"], "Evidence")
        self.assertEqual(
            [row["title"] for row in response.data["lessons"]], ["First", "Second"]
        )

    def test_draft_is_404_for_a_learner(self) -> None:
        draft = make_course(self.instructor, "Draft course")
        self.login(self.learner)
        self.assertEqual(self.detail(draft).status_code, NOT_FOUND)

    def test_draft_is_404_for_another_instructor(self) -> None:
        draft = make_course(self.instructor, "Draft course")
        self.login(make_instructor("rival"))
        self.assertEqual(self.detail(draft).status_code, NOT_FOUND)

    def test_owner_previews_their_own_draft(self) -> None:
        draft = make_course(self.instructor, "Draft course")
        self.login(self.instructor)
        self.assertEqual(self.detail(draft).status_code, OK)

    def test_enrolled_learner_keeps_access_after_unpublish(self) -> None:
        enrol(self.learner, self.course)
        self.course.status = Course.Status.DRAFT
        self.course.save()
        self.login(self.learner)
        self.assertEqual(self.detail(self.course).status_code, OK)


class EnrolAPITests(APITestCase):
    """Enrolling is idempotent and only allowed on a published course."""

    def enrol_url(self, course: Course) -> str:
        return reverse("api:course-enrol", args=[course.pk])

    def test_enrol_creates_a_row(self) -> None:
        self.login(self.learner)
        response = self.client.post(self.enrol_url(self.course), format="json")
        self.assertEqual(response.status_code, CREATED)
        self.assertTrue(
            Enrollment.objects.filter(user=self.learner, course=self.course).exists()
        )

    def test_enrol_is_idempotent(self) -> None:
        self.login(self.learner)
        self.client.post(self.enrol_url(self.course), format="json")
        response = self.client.post(self.enrol_url(self.course), format="json")
        self.assertEqual(response.status_code, OK)
        self.assertEqual(
            Enrollment.objects.filter(user=self.learner, course=self.course).count(), 1
        )

    def test_enrol_in_a_draft_is_404(self) -> None:
        draft = make_course(self.instructor, "Draft course")
        self.login(self.learner)
        self.assertEqual(
            self.client.post(self.enrol_url(draft), format="json").status_code,
            NOT_FOUND,
        )

    def test_enrol_requires_authentication(self) -> None:
        response = self.client.post(self.enrol_url(self.course), format="json")
        self.assertEqual(response.status_code, UNAUTHORIZED)


class MyEnrolmentsAPITests(APITestCase):
    """The list shows only the caller's enrolments, with derived progress."""

    def test_lists_only_own_enrolments(self) -> None:
        enrol(self.learner, self.course)
        enrol(make_learner("other"), self.course)
        self.login(self.learner)
        response = self.client.get(reverse("api:my-enrolments"))
        self.assertEqual(response.status_code, OK)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["course"]["title"], "Evidence")
        self.assertEqual(row["progress"], {"completed": 0, "total": 2, "percent": 0})

    def test_progress_reflects_completed_lessons(self) -> None:
        enrollment = enrol(self.learner, self.course)
        mark_lesson_complete(enrollment, self.lesson)
        self.login(self.learner)
        response = self.client.get(reverse("api:my-enrolments"))
        self.assertEqual(
            response.data["results"][0]["progress"],
            {"completed": 1, "total": 2, "percent": 50},
        )


class LessonCompleteAPITests(APITestCase):
    """Marking complete: enrolment needed, order enforced, idempotent."""

    def complete_url(self, course: Course, lesson: Lesson) -> str:
        return reverse("api:lesson-complete", args=[course.pk, lesson.pk])

    def test_enrolled_learner_completes_a_lesson(self) -> None:
        enrol(self.learner, self.course)
        self.login(self.learner)
        response = self.client.post(
            self.complete_url(self.course, self.lesson), format="json"
        )
        self.assertEqual(response.status_code, CREATED)
        self.assertTrue(response.data["created"])
        self.assertEqual(response.data["progress"]["percent"], 50)
        self.assertTrue(
            LessonProgress.objects.filter(
                enrollment__user=self.learner, lesson=self.lesson
            ).exists()
        )

    def test_completing_twice_does_not_duplicate(self) -> None:
        enrol(self.learner, self.course)
        self.login(self.learner)
        self.client.post(self.complete_url(self.course, self.lesson), format="json")
        response = self.client.post(
            self.complete_url(self.course, self.lesson), format="json"
        )
        self.assertEqual(response.status_code, OK)
        self.assertFalse(response.data["created"])
        self.assertEqual(
            LessonProgress.objects.filter(
                enrollment__user=self.learner, lesson=self.lesson
            ).count(),
            1,
        )

    def test_unenrolled_learner_gets_403(self) -> None:
        self.login(self.learner)
        response = self.client.post(
            self.complete_url(self.course, self.lesson), format="json"
        )
        self.assertEqual(response.status_code, FORBIDDEN)

    def test_instructor_getting_the_course_is_not_enrolled(self) -> None:
        self.login(self.instructor)
        response = self.client.post(
            self.complete_url(self.course, self.lesson), format="json"
        )
        self.assertEqual(response.status_code, FORBIDDEN)

    def test_draft_is_404_for_a_non_owner(self) -> None:
        draft = make_course(self.instructor, "Draft course")
        lesson = make_lesson(draft, "Intro")
        self.login(self.learner)
        response = self.client.post(self.complete_url(draft, lesson), format="json")
        self.assertEqual(response.status_code, NOT_FOUND)

    def test_lesson_from_another_course_is_404(self) -> None:
        other = publish(make_course(self.instructor, "Other course"))
        stray = make_lesson(other, "Elsewhere")
        enrol(self.learner, self.course)
        self.login(self.learner)
        response = self.client.post(
            self.complete_url(self.course, stray), format="json"
        )
        self.assertEqual(response.status_code, NOT_FOUND)

    def test_locked_lesson_is_409(self) -> None:
        self.course.enforce_order = True
        self.course.save()
        enrol(self.learner, self.course)
        self.login(self.learner)
        response = self.client.post(
            self.complete_url(self.course, self.second_lesson), format="json"
        )
        self.assertEqual(response.status_code, CONFLICT)

    def test_finishing_the_first_unlocks_the_second(self) -> None:
        self.course.enforce_order = True
        self.course.save()
        enrol(self.learner, self.course)
        self.login(self.learner)
        first = self.client.post(
            self.complete_url(self.course, self.lesson), format="json"
        )
        self.assertEqual(first.status_code, CREATED)
        second = self.client.post(
            self.complete_url(self.course, self.second_lesson), format="json"
        )
        self.assertEqual(second.status_code, CREATED)


class WriteScopeTests(APITestCase):
    """R7's "instructor-only writes" stay true: the API adds no authoring.

    Course content is read-only over the API (collection and detail refuse
    every unsafe method); authoring remains on the web pages, where
    ``InstructorRequiredMixin`` and ``OwnerCourseMixin`` protect it.  The only
    writes the API exposes are the learner actions (enrol, mark complete).
    """

    def test_course_collection_is_read_only(self) -> None:
        self.login(self.instructor)
        url = reverse("api:course-list")
        self.assertEqual(
            self.client.post(url, {"title": "New"}, format="json").status_code,
            METHOD_NOT_ALLOWED,
        )

    def test_course_detail_is_read_only(self) -> None:
        self.login(self.instructor)
        url = reverse("api:course-detail", args=[self.course.pk])
        self.assertEqual(
            self.client.put(url, {"title": "x"}, format="json").status_code,
            METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.patch(url, {"title": "x"}, format="json").status_code,
            METHOD_NOT_ALLOWED,
        )
        self.assertEqual(self.client.delete(url).status_code, METHOD_NOT_ALLOWED)

    def test_enrolments_are_read_only(self) -> None:
        self.login(self.learner)
        self.assertEqual(
            self.client.post(
                reverse("api:my-enrolments"), {}, format="json"
            ).status_code,
            METHOD_NOT_ALLOWED,
        )


class ThrottleTests(APITestCase):
    """Per-user throttling returns 429 once the rate is exceeded.

    ``SimpleRateThrottle.THROTTLE_RATES`` is a class attribute captured at
    import time, so ``override_settings`` alone would not change the rate;
    patching the attribute directly does.
    """

    @mock.patch.object(UserRateThrottle, "THROTTLE_RATES", {"user": "2/minute"})
    def test_user_is_throttled_after_the_limit(self) -> None:
        cache.clear()
        self.login(self.learner)
        url = reverse("api:course-list")
        self.assertEqual(self.client.get(url).status_code, OK)
        self.assertEqual(self.client.get(url).status_code, OK)
        self.assertEqual(self.client.get(url).status_code, TOO_MANY_REQUESTS)


class SchemaTests(APITestCase):
    """The OpenAPI schema and the Swagger UI are served."""

    def test_schema_is_served(self) -> None:
        response = self.client.get(reverse("schema"), HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, OK)
        self.assertContains(response, "api/v1/courses")

    def test_swagger_ui_is_served(self) -> None:
        self.assertEqual(self.client.get(reverse("swagger-ui")).status_code, OK)
