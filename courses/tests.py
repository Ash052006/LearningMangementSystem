"""Tests for courses: model constraints plus instructor course/lesson views.

Password hashing is skipped where possible: these tests authenticate with
``force_login`` (which does not check the password), so password hashing would
only slow the suite down.
"""

import csv
import shutil
import tempfile
import uuid
from decimal import Decimal
from io import StringIO

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from quizzes.models import Choice, Question, Quiz, QuizAttempt
from quizzes.services import submit_attempt

from .models import Certificate, Course, Enrollment, Lesson, LessonProgress
from .services import (
    LearnerStat,
    can_access_lesson,
    certificate_eligible,
    course_learner_stats,
    course_progress,
    course_question_stats,
    enrol,
    first_blocking_lesson,
    issue_certificate_if_eligible,
    mark_lesson_complete,
    move_lesson_up,
    unlocked_lesson_ids,
)
from .validators import MAX_ATTACHMENT_SIZE
from .views import CatalogueView, _csv_safe

User = get_user_model()

FORBIDDEN = 403
NOT_FOUND = 404


def make_instructor(username: str = "inst") -> User:
    """Create an instructor user.

    No password is set on purpose: these tests never authenticate, and
    password hashing would dominate the runtime.
    """
    return User.objects.create_user(username=username, role=User.Role.INSTRUCTOR)


def make_learner(username: str = "learner") -> User:
    """Create a learner user (no password, as above)."""
    return User.objects.create_user(username=username)


def make_course(instructor: User, title: str = "Law 101") -> Course:
    """Create a draft course owned by ``instructor``."""
    return Course.objects.create(instructor=instructor, title=title)


def make_lesson(course: Course, title: str = "Intro", **kwargs: object) -> Lesson:
    """Create a lesson inside ``course``, auto-assigning its position."""
    return Lesson.objects.create(course=course, title=title, content="...", **kwargs)


class CourseModelTests(TestCase):
    """Course fields, helpers, ordering and the instructor PROTECT rule."""

    def setUp(self) -> None:
        self.instructor = make_instructor()

    def test_str_returns_title(self) -> None:
        course = Course.objects.create(instructor=self.instructor, title="Law 101")
        self.assertEqual(str(course), "Law 101")

    def test_default_status_is_draft(self) -> None:
        course = Course.objects.create(instructor=self.instructor, title="Law 101")
        self.assertEqual(course.status, Course.Status.DRAFT)

    def test_is_published_reflects_status(self) -> None:
        course = Course.objects.create(instructor=self.instructor, title="Law 101")
        self.assertFalse(course.is_published)
        course.status = Course.Status.PUBLISHED
        self.assertTrue(course.is_published)

    def test_publishable_false_without_lessons(self) -> None:
        course = Course.objects.create(instructor=self.instructor, title="Law 101")
        self.assertFalse(course.publishable)

    def test_publishable_true_with_lesson(self) -> None:
        course = Course.objects.create(instructor=self.instructor, title="Law 101")
        Lesson.objects.create(course=course, title="Intro", content="...")
        self.assertTrue(course.publishable)

    def test_publishable_false_after_last_lesson_deleted(self) -> None:
        lesson = Lesson.objects.create(
            course=Course.objects.create(instructor=self.instructor, title="Law 101"),
            title="Intro",
            content="...",
        )
        lesson.delete()
        self.assertFalse(lesson.course.publishable)

    def test_ordering_newest_first(self) -> None:
        first = Course.objects.create(instructor=self.instructor, title="Old")
        second = Course.objects.create(instructor=self.instructor, title="New")
        self.assertEqual(list(Course.objects.all()), [second, first])

    def test_status_is_indexed(self) -> None:
        field = Course._meta.get_field("status")
        self.assertTrue(field.db_index)

    def test_deleting_instructor_with_courses_is_protected(self) -> None:
        Course.objects.create(instructor=self.instructor, title="Law 101")
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.instructor.delete()

    def test_deleting_instructor_without_courses_cascades(self) -> None:
        spare = make_instructor("spare")
        spare.delete()
        self.assertFalse(User.objects.filter(username="spare").exists())


class LessonModelTests(TestCase):
    """Lesson positions, ordering and the (course, position) constraint."""

    def setUp(self) -> None:
        self.course = Course.objects.create(
            instructor=make_instructor(), title="Law 101"
        )

    def test_str_shows_position_and_title(self) -> None:
        lesson = Lesson.objects.create(course=self.course, title="Intro", content="...")
        self.assertEqual(str(lesson), "1. Intro")

    def test_position_auto_assigned_sequentially(self) -> None:
        one = Lesson.objects.create(course=self.course, title="One", content="x")
        two = Lesson.objects.create(course=self.course, title="Two", content="x")
        three = Lesson.objects.create(course=self.course, title="Three", content="x")
        self.assertEqual([one.position, two.position, three.position], [1, 2, 3])

    def test_explicit_position_kept_and_next_follows_it(self) -> None:
        ten = Lesson.objects.create(
            course=self.course, title="Ten", content="x", position=10
        )
        auto = Lesson.objects.create(course=self.course, title="Auto", content="x")
        self.assertEqual(ten.position, 10)
        self.assertEqual(auto.position, 11)

    def test_ordering_by_position(self) -> None:
        second = Lesson.objects.create(
            course=self.course, title="Second", content="x", position=2
        )
        first = Lesson.objects.create(
            course=self.course, title="First", content="x", position=1
        )
        self.assertEqual(list(self.course.lessons.all()), [first, second])

    def test_unique_position_per_course(self) -> None:
        Lesson.objects.create(course=self.course, title="One", content="x", position=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Lesson.objects.create(
                    course=self.course, title="Dup", content="x", position=1
                )

    def test_same_position_allowed_in_another_course(self) -> None:
        other = Course.objects.create(instructor=make_instructor("i2"), title="Other")
        Lesson.objects.create(course=self.course, title="One", content="x", position=1)
        other_lesson = Lesson.objects.create(
            course=other, title="One", content="x", position=1
        )
        self.assertEqual(other_lesson.position, 1)


class LessonAttachmentValidatorTests(TestCase):
    """Size and extension validators on Lesson.attachment."""

    def setUp(self) -> None:
        self.course = Course.objects.create(
            instructor=make_instructor(), title="Law 101"
        )

    def _lesson_with_attachment(self, name: str, payload: bytes) -> Lesson:
        return Lesson(
            course=self.course,
            title="Intro",
            content="x",
            position=1,
            attachment=SimpleUploadedFile(name, payload),
        )

    def test_attachment_too_large_rejected(self) -> None:
        lesson = self._lesson_with_attachment(
            "big.pdf", b"x" * (MAX_ATTACHMENT_SIZE + 1)
        )
        with self.assertRaisesMessage(ValidationError, "MB or smaller"):
            lesson.full_clean()

    def test_attachment_at_size_limit_accepted(self) -> None:
        # Only files *exceeding* 10 MB are rejected; exactly 10 MB is fine.
        lesson = self._lesson_with_attachment("big.pdf", b"x" * MAX_ATTACHMENT_SIZE)
        lesson.full_clean()  # Should not raise.

    def test_attachment_bad_extension_rejected(self) -> None:
        lesson = self._lesson_with_attachment("evil.exe", b"MZ")
        with self.assertRaisesMessage(ValidationError, "Unsupported file type"):
            lesson.full_clean()

    def test_attachment_missing_extension_rejected(self) -> None:
        lesson = self._lesson_with_attachment("README", b"data")
        with self.assertRaisesMessage(ValidationError, "Unsupported file type"):
            lesson.full_clean()

    def test_valid_attachment_passes(self) -> None:
        lesson = self._lesson_with_attachment("notes.pdf", b"%PDF-1.4 ...")
        lesson.full_clean()  # Should not raise.

    def test_attachment_is_optional(self) -> None:
        Lesson.objects.create(course=self.course, title="Bare", content="x")
        lesson = self.course.lessons.get(title="Bare")
        lesson.full_clean()  # Should not raise.

    def test_extension_check_is_case_insensitive(self) -> None:
        lesson = self._lesson_with_attachment("IMAGE.JPG", b"\xff\xd8")
        lesson.full_clean()  # Should not raise.


class EnrollmentModelTests(TestCase):
    """One enrolment per (user, course)."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="learner")
        self.course = Course.objects.create(
            instructor=make_instructor(), title="Law 101"
        )

    def test_str_shows_user_and_course(self) -> None:
        enrollment = Enrollment.objects.create(user=self.user, course=self.course)
        self.assertIn("learner", str(enrollment))
        self.assertIn("Law 101", str(enrollment))

    def test_duplicate_user_course_rejected(self) -> None:
        Enrollment.objects.create(user=self.user, course=self.course)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Enrollment.objects.create(user=self.user, course=self.course)

    def test_same_user_other_course_allowed(self) -> None:
        other = Course.objects.create(instructor=make_instructor("i2"), title="Other")
        Enrollment.objects.create(user=self.user, course=self.course)
        Enrollment.objects.create(user=self.user, course=other)
        self.assertEqual(self.user.enrollments.count(), 2)


class LessonProgressModelTests(TestCase):
    """One progress row per (enrollment, lesson) plus clean() consistency."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="learner")
        self.course = Course.objects.create(
            instructor=make_instructor(), title="Law 101"
        )
        self.lesson = Lesson.objects.create(
            course=self.course, title="Intro", content="x"
        )
        self.enrollment = Enrollment.objects.create(user=self.user, course=self.course)

    def test_str_mentions_lesson_and_user(self) -> None:
        progress = LessonProgress.objects.create(
            enrollment=self.enrollment, lesson=self.lesson
        )
        self.assertIn("Intro", str(progress))
        self.assertIn("learner", str(progress))

    def test_duplicate_enrollment_lesson_rejected(self) -> None:
        LessonProgress.objects.create(enrollment=self.enrollment, lesson=self.lesson)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LessonProgress.objects.create(
                    enrollment=self.enrollment, lesson=self.lesson
                )

    def test_clean_accepts_lesson_from_same_course(self) -> None:
        progress = LessonProgress(enrollment=self.enrollment, lesson=self.lesson)
        progress.full_clean()  # Should not raise.

    def test_clean_rejects_lesson_from_another_course(self) -> None:
        other_course = Course.objects.create(
            instructor=make_instructor("i2"), title="Other"
        )
        foreign_lesson = Lesson.objects.create(
            course=other_course, title="Elsewhere", content="x"
        )
        progress = LessonProgress(enrollment=self.enrollment, lesson=foreign_lesson)
        with self.assertRaises(ValidationError) as ctx:
            progress.full_clean()
        self.assertIn("lesson", ctx.exception.error_dict)

    def test_deleting_lesson_cascades_progress(self) -> None:
        LessonProgress.objects.create(enrollment=self.enrollment, lesson=self.lesson)
        self.lesson.delete()
        self.assertFalse(LessonProgress.objects.exists())

    def test_deleting_course_removes_lessons_enrollments_and_progress(self) -> None:
        LessonProgress.objects.create(enrollment=self.enrollment, lesson=self.lesson)
        self.course.delete()
        self.assertFalse(Lesson.objects.exists())
        self.assertFalse(Enrollment.objects.exists())
        self.assertFalse(LessonProgress.objects.exists())


class InstructorViewTestCase(TestCase):
    """Shared fixtures for the instructor course/lesson view tests."""

    def setUp(self) -> None:
        self.instructor = make_instructor("owner")
        self.other_instructor = make_instructor("rival")
        self.learner = make_learner()
        self.course = make_course(self.instructor)
        self.lesson = make_lesson(self.course, "First")
        self.client.force_login(self.instructor)

    def _login_as(self, user: User) -> None:
        self.client.force_login(user)


class CourseAccessControlTests(InstructorViewTestCase):
    """Role checks (403) and ownership checks (404) for course URLs."""

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.client.logout()
        url = reverse("courses:course_create")
        response = self.client.get(url)
        self.assertRedirects(response, f"{reverse('accounts:login')}?next={url}")

    def test_learner_gets_403_on_create_edit_and_delete(self) -> None:
        self._login_as(self.learner)
        self.assertEqual(
            self.client.get(reverse("courses:course_create")).status_code,
            FORBIDDEN,
        )
        self.assertEqual(
            self.client.get(
                reverse("courses:course_edit", args=[self.course.pk])
            ).status_code,
            FORBIDDEN,
        )
        self.assertEqual(
            self.client.post(
                reverse("courses:course_delete", args=[self.course.pk])
            ).status_code,
            FORBIDDEN,
        )

    def test_learner_gets_403_on_listing_and_publish(self) -> None:
        self._login_as(self.learner)
        self.assertEqual(
            self.client.get(reverse("courses:instructor_course_list")).status_code,
            FORBIDDEN,
        )
        self.assertEqual(
            self.client.post(
                reverse("courses:course_toggle_publish", args=[self.course.pk])
            ).status_code,
            FORBIDDEN,
        )

    def test_other_instructor_gets_404_on_course_urls(self) -> None:
        self._login_as(self.other_instructor)
        cases = [
            ("get", reverse("courses:course_detail", args=[self.course.pk])),
            ("get", reverse("courses:course_edit", args=[self.course.pk])),
            ("get", reverse("courses:course_delete", args=[self.course.pk])),
            (
                "post",
                reverse("courses:course_toggle_publish", args=[self.course.pk]),
            ),
        ]
        for method, url in cases:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, NOT_FOUND)

    def test_other_instructor_gets_404_on_lesson_urls(self) -> None:
        self._login_as(self.other_instructor)
        lesson_urls = [
            ("get", reverse("courses:lesson_create", args=[self.course.pk])),
            (
                "get",
                reverse("courses:lesson_edit", args=[self.course.pk, self.lesson.pk]),
            ),
            (
                "get",
                reverse("courses:lesson_delete", args=[self.course.pk, self.lesson.pk]),
            ),
            (
                "post",
                reverse(
                    "courses:lesson_move_up", args=[self.course.pk, self.lesson.pk]
                ),
            ),
        ]
        for method, url in lesson_urls:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, NOT_FOUND)

    def test_list_shows_only_own_courses(self) -> None:
        other = make_course(self.other_instructor, "Rival course")
        response = self.client.get(reverse("courses:instructor_course_list"))
        self.assertContains(response, self.course.title)
        self.assertNotContains(response, other.title)


class CourseCrudTests(InstructorViewTestCase):
    """Create, update and delete for a course owned by the instructor."""

    def test_create_sets_instructor_from_request(self) -> None:
        response = self.client.post(
            reverse("courses:course_create"),
            {"title": "Evidence", "description": "d", "enforce_order": ""},
        )
        self.assertRedirects(response, reverse("courses:instructor_course_list"))
        course = Course.objects.get(title="Evidence")
        self.assertEqual(course.instructor, self.instructor)
        self.assertEqual(course.status, Course.Status.DRAFT)

    def test_create_never_trusts_an_instructor_field_in_the_post(self) -> None:
        self.client.post(
            reverse("courses:course_create"),
            {
                "title": "Sneaky",
                "description": "",
                "enforce_order": "",
                "instructor": str(self.other_instructor.pk),
            },
        )
        self.assertEqual(Course.objects.get(title="Sneaky").instructor, self.instructor)

    def test_edit_updates_own_course(self) -> None:
        response = self.client.post(
            reverse("courses:course_edit", args=[self.course.pk]),
            {"title": "Renamed", "description": "", "enforce_order": "on"},
        )
        self.assertRedirects(
            response, reverse("courses:course_detail", args=[self.course.pk])
        )
        self.course.refresh_from_db()
        self.assertEqual(self.course.title, "Renamed")
        self.assertTrue(self.course.enforce_order)

    def test_delete_course_blocked_when_quiz_has_answers(self) -> None:
        """ProtectedError when AttemptAnswer exists -> error + redirect."""

        from quizzes.models import AttemptAnswer, Choice, Question, Quiz

        quiz = Quiz.objects.create(course=self.course)
        q = Question.objects.create(quiz=quiz, text="q", position=1)
        c = Choice.objects.create(question=q, text="a", is_correct=True)
        enrollment = enrol(self.learner, self.course)
        from quizzes.services import submit_attempt

        try:
            submit_attempt(enrollment, quiz, {f"question_{q.pk}": str(c.pk)})
        except Exception:
            # If not gradable? but has choices; try directly by creating attempt
            pass
        # Easier: create attempt+answer to force PROTECT
        from quizzes.models import QuizAttempt

        qa = QuizAttempt.objects.create(
            enrollment=enrollment,
            quiz=quiz,
            score=1,
            total=1,
            percent=100,
            passed=True,
        )
        AttemptAnswer.objects.create(attempt=qa, question=q, selected_choice=c)
        response = self.client.post(
            reverse("courses:course_delete", args=[self.course.pk]), follow=True
        )
        self.assertRedirects(
            response, reverse("courses:course_detail", args=[self.course.pk])
        )
        messages = [m.message for m in response.context["messages"]]
        self.assertTrue(any("cannot be deleted" in m for m in messages))

    def test_delete_confirmation_page_lists_the_course(self) -> None:
        response = self.client.get(
            reverse("courses:course_delete", args=[self.course.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.course.title)


class CoursePublishToggleTests(InstructorViewTestCase):
    """Publishing requires at least one lesson; unpublishing always works."""

    def test_cannot_publish_empty_course(self) -> None:
        empty = make_course(self.instructor, "Empty")
        response = self.client.post(
            reverse("courses:course_toggle_publish", args=[empty.pk]), follow=True
        )
        empty.refresh_from_db()
        self.assertEqual(empty.status, Course.Status.DRAFT)
        self.assertContains(response, "at least one lesson")

    def test_publish_course_with_a_lesson(self) -> None:
        self.client.post(
            reverse("courses:course_toggle_publish", args=[self.course.pk])
        )
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PUBLISHED)

    def test_toggle_again_unpublishes(self) -> None:
        self.course.status = Course.Status.PUBLISHED
        self.course.save()
        self.client.post(
            reverse("courses:course_toggle_publish", args=[self.course.pk])
        )
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.DRAFT)

    def test_toggle_rejects_get(self) -> None:
        response = self.client.get(
            reverse("courses:course_toggle_publish", args=[self.course.pk])
        )
        self.assertEqual(response.status_code, 405)


class LessonCrudTests(InstructorViewTestCase):
    """Lesson create, edit and delete under a course the instructor owns."""

    def test_create_lesson_appends_at_next_position(self) -> None:
        response = self.client.post(
            reverse("courses:lesson_create", args=[self.course.pk]),
            {"title": "Second", "content": "body", "video_url": ""},
        )
        self.assertRedirects(
            response, reverse("courses:course_detail", args=[self.course.pk])
        )
        second = Lesson.objects.get(course=self.course, title="Second")
        self.assertEqual(second.position, 2)

    def test_edit_lesson(self) -> None:
        response = self.client.post(
            reverse("courses:lesson_edit", args=[self.course.pk, self.lesson.pk]),
            {"title": "Amended", "content": "new", "video_url": ""},
        )
        self.assertRedirects(
            response, reverse("courses:course_detail", args=[self.course.pk])
        )
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.title, "Amended")

    def test_delete_lesson_closes_the_position_gap(self) -> None:
        second = make_lesson(self.course, "Second")
        third = make_lesson(self.course, "Third")
        self.client.post(
            reverse("courses:lesson_delete", args=[self.course.pk, second.pk])
        )
        self.assertFalse(Lesson.objects.filter(pk=second.pk).exists())
        self.assertEqual(
            list(self.course.lessons.values_list("position", flat=True)),
            [1, 2],
        )
        third.refresh_from_db()
        self.assertEqual(third.position, 2)


class LessonOrderingTests(InstructorViewTestCase):
    """Move up/down swaps neighbouring positions inside a transaction."""

    def setUp(self) -> None:
        super().setUp()
        self.second = make_lesson(self.course, "Second")
        self.third = make_lesson(self.course, "Third")

    def _positions(self) -> list[int]:
        return list(self.course.lessons.values_list("position", flat=True))

    def test_move_up_swaps_with_previous(self) -> None:
        self.client.post(
            reverse("courses:lesson_move_up", args=[self.course.pk, self.second.pk])
        )
        self.second.refresh_from_db()
        self.lesson.refresh_from_db()
        self.assertEqual(self.second.position, 1)
        self.assertEqual(self.lesson.position, 2)
        self.assertEqual(self._positions(), [1, 2, 3])

    def test_move_down_swaps_with_next(self) -> None:
        self.client.post(
            reverse("courses:lesson_move_down", args=[self.course.pk, self.lesson.pk])
        )
        self.lesson.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.second.position, 1)
        self.assertEqual(self.lesson.position, 2)
        self.assertEqual(self._positions(), [1, 2, 3])

    def test_move_up_at_top_is_a_no_op(self) -> None:
        self.client.post(
            reverse("courses:lesson_move_up", args=[self.course.pk, self.lesson.pk])
        )
        self.assertEqual(self._positions(), [1, 2, 3])

    def test_move_down_at_bottom_is_a_no_op(self) -> None:
        self.client.post(
            reverse("courses:lesson_move_down", args=[self.course.pk, self.third.pk])
        )
        self.assertEqual(self._positions(), [1, 2, 3])

    def test_move_rejects_get(self) -> None:
        response = self.client.get(
            reverse("courses:lesson_move_up", args=[self.course.pk, self.second.pk])
        )
        self.assertEqual(response.status_code, 405)


class LessonFormValidationTests(InstructorViewTestCase):
    """The lesson form restricts video URLs to http/https and checks files."""

    def test_http_and_https_video_urls_accepted(self) -> None:
        for url in ("http://example.com/v", "https://example.com/v"):
            with self.subTest(url=url):
                response = self.client.post(
                    reverse("courses:lesson_create", args=[self.course.pk]),
                    {"title": f"L {url}", "content": "x", "video_url": url},
                )
                self.assertEqual(response.status_code, 302)

    def test_ftp_video_url_rejected(self) -> None:
        response = self.client.post(
            reverse("courses:lesson_create", args=[self.course.pk]),
            {"title": "Bad", "content": "x", "video_url": "ftp://example.com/v"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Lesson.objects.filter(title="Bad").exists())

    def test_oversized_attachment_rejected(self) -> None:
        response = self.client.post(
            reverse("courses:lesson_create", args=[self.course.pk]),
            {
                "title": "Big",
                "content": "x",
                "video_url": "",
                "attachment": SimpleUploadedFile(
                    "big.pdf", b"x" * (MAX_ATTACHMENT_SIZE + 1)
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Lesson.objects.filter(title="Big").exists())


def publish(course: Course) -> Course:
    """Set a course to published (used by the learner-facing tests)."""
    course.status = Course.Status.PUBLISHED
    course.save()
    return course


class LearnerFlowTestCase(TestCase):
    """Shared fixtures for the learner-facing flow tests."""

    def setUp(self) -> None:
        self.instructor = make_instructor("teacher")
        self.learner = make_learner("student")
        self.course = publish(make_course(self.instructor, "Evidence"))
        self.course.description = "How to handle evidence."
        self.course.save()
        self.lesson_one = make_lesson(self.course, "First")
        self.lesson_two = make_lesson(self.course, "Second")
        self.client.force_login(self.learner)

    def _enrol(self) -> Enrollment:
        return enrol(self.learner, self.course)

    def _course_url(self) -> str:
        return reverse("courses:learner_course_detail", args=[self.course.pk])

    def _lesson_url(self, lesson: Lesson) -> str:
        return reverse(
            "courses:learner_lesson_detail", args=[self.course.pk, lesson.pk]
        )


class CatalogueTests(LearnerFlowTestCase):
    """The catalogue lists published courses, paginated, without N+1."""

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.client.logout()
        url = reverse("courses:catalogue")
        self.assertRedirects(
            response=self.client.get(url),
            expected_url=f"{reverse('accounts:login')}?next={url}",
        )

    def test_only_published_courses_are_listed(self) -> None:
        draft = make_course(self.instructor, "Draft course")
        response = self.client.get(reverse("courses:catalogue"))
        self.assertContains(response, self.course.title)
        self.assertNotContains(response, draft.title)

    def test_shows_lesson_count(self) -> None:
        response = self.client.get(reverse("courses:catalogue"))
        self.assertContains(response, "2 lessons")

    def test_pagination_splits_the_list(self) -> None:
        for index in range(CatalogueView.paginate_by):
            publish(make_course(self.instructor, f"Extra {index}"))
        response = self.client.get(reverse("courses:catalogue"))
        self.assertTrue(response.context["is_paginated"])
        self.assertEqual(len(response.context["courses"]), CatalogueView.paginate_by)


class CatalogueFilterTests(LearnerFlowTestCase):
    """Search, instructor filter, sorting and pagination on the catalogue."""

    def setUp(self) -> None:
        super().setUp()
        self.other = make_instructor("other")
        self.contracts = publish(make_course(self.other, "Contracts"))
        self.contracts.description = "Drafting and interpreting agreements."
        self.contracts.save(update_fields=["description"])
        self.draft = make_course(self.instructor, "Evidence (draft)")
        self.draft.description = "Contains the secret token zebra."
        self.draft.save(update_fields=["description"])

    def catalogue(self, **params: object) -> HttpResponse:
        return self.client.get(reverse("courses:catalogue"), params)

    def listed_titles(self, response: HttpResponse) -> list[str]:
        return [course.title for course in response.context["courses"]]

    def test_search_matches_the_title(self) -> None:
        self.assertEqual(
            self.listed_titles(self.catalogue(q="contract")), ["Contracts"]
        )

    def test_search_matches_the_description(self) -> None:
        self.assertEqual(
            self.listed_titles(self.catalogue(q="agreements")), ["Contracts"]
        )

    def test_search_is_case_insensitive(self) -> None:
        self.assertEqual(self.listed_titles(self.catalogue(q="EVIDENCE")), ["Evidence"])

    def test_search_never_returns_a_draft(self) -> None:
        response = self.catalogue(q="zebra")
        self.assertEqual(self.listed_titles(response), [])
        self.assertContains(response, "No published courses match")

    def test_search_does_not_leak_a_draft_title(self) -> None:
        response = self.catalogue(q="draft")
        self.assertNotContains(response, self.draft.title)

    def test_filter_by_instructor(self) -> None:
        response = self.catalogue(instructor=self.other.pk)
        self.assertEqual(self.listed_titles(response), ["Contracts"])

    def test_filter_by_the_owning_instructor(self) -> None:
        response = self.catalogue(instructor=self.instructor.pk)
        self.assertEqual(self.listed_titles(response), ["Evidence"])

    def test_sort_by_title(self) -> None:
        publish(make_course(self.other, "Arbitration"))
        titles = self.listed_titles(self.catalogue(sort="title"))
        self.assertEqual(titles, sorted(titles))

    def test_default_sort_is_newest_first(self) -> None:
        publish(make_course(self.instructor, "Zoning"))
        self.assertEqual(self.listed_titles(self.catalogue())[0], "Zoning")

    def test_invalid_params_do_not_crash(self) -> None:
        cases = [
            {"q": "x" * 500},
            {"instructor": "not-a-number"},
            {"instructor": "999999"},
            {"sort": "sideways"},
            {"page": "abc"},
            {"page": "0"},
            {"page": "-3"},
            {"q": "evidence", "sort": "title", "page": "abc", "instructor": "nope"},
        ]
        for params in cases:
            with self.subTest(params=params):
                self.assertEqual(self.catalogue(**params).status_code, 200)

    def test_out_of_range_page_is_clamped(self) -> None:
        for index in range(CatalogueView.paginate_by + 2):
            publish(make_course(self.instructor, f"Extra {index}"))
        response = self.catalogue(page="9999")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["page_obj"].number,
            response.context["paginator"].num_pages,
        )

    def test_pagination_links_keep_the_filters(self) -> None:
        for index in range(CatalogueView.paginate_by + 2):
            publish(make_course(self.instructor, f"Evidence extra {index}"))
        response = self.catalogue(q="evidence")
        self.assertTrue(response.context["is_paginated"])
        self.assertContains(response, "q=evidence")
        self.assertContains(response, "page=2")

    def test_querystring_keeps_only_the_valid_filters(self) -> None:
        response = self.catalogue(
            q="evidence", sort="bogus", instructor="nope", page="abc"
        )
        self.assertEqual(response.context["querystring"], "q=evidence")

    def test_instructor_dropdown_lists_instructors(self) -> None:
        form = self.catalogue().context["filter_form"]
        self.assertIn(self.other, form.fields["instructor"].queryset)


class LearnerCourseDetailTests(LearnerFlowTestCase):
    """Learner course detail: published only, with enrol or progress."""

    def test_draft_course_is_hidden(self) -> None:
        draft = make_course(self.instructor, "Hidden")
        response = self.client.get(
            reverse("courses:learner_course_detail", args=[draft.pk])
        )
        self.assertEqual(response.status_code, NOT_FOUND)

    def test_shows_description_and_lesson_titles(self) -> None:
        response = self.client.get(self._course_url())
        self.assertContains(response, "How to handle evidence.")
        self.assertContains(response, self.lesson_one.title)
        self.assertContains(response, self.lesson_two.title)

    def test_enrol_form_shown_when_not_enrolled(self) -> None:
        response = self.client.get(self._course_url())
        self.assertContains(response, reverse("courses:enrol", args=[self.course.pk]))
        self.assertNotContains(response, "lessons done")

    def test_progress_and_continue_shown_when_enrolled(self) -> None:
        enrollment = self._enrol()
        LessonProgress.objects.create(enrollment=enrollment, lesson=self.lesson_one)
        response = self.client.get(self._course_url())
        self.assertContains(response, "1 of 2 lessons done (50%)")
        # Continue points at the first unfinished lesson.
        self.assertContains(response, self._lesson_url(self.lesson_two))
        self.assertNotContains(
            response, reverse("courses:enrol", args=[self.course.pk])
        )


class EnrolTests(LearnerFlowTestCase):
    """Enrolment is POST-only, published-only and idempotent."""

    def test_enrol_creates_enrollment_and_redirects(self) -> None:
        response = self.client.post(reverse("courses:enrol", args=[self.course.pk]))
        self.assertRedirects(response, self._course_url())
        self.assertTrue(
            Enrollment.objects.filter(user=self.learner, course=self.course).exists()
        )

    def test_double_enrol_creates_one_row(self) -> None:
        url = reverse("courses:enrol", args=[self.course.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(
            Enrollment.objects.filter(user=self.learner, course=self.course).count(), 1
        )

    def test_draft_course_cannot_be_joined(self) -> None:
        draft = make_course(self.instructor, "Hidden")
        response = self.client.post(reverse("courses:enrol", args=[draft.pk]))
        self.assertEqual(response.status_code, NOT_FOUND)
        self.assertFalse(Enrollment.objects.filter(course=draft).exists())

    def test_enrol_requires_login(self) -> None:
        self.client.logout()
        url = reverse("courses:enrol", args=[self.course.pk])
        self.assertRedirects(
            self.client.post(url), f"{reverse('accounts:login')}?next={url}"
        )

    def test_enrol_rejects_get(self) -> None:
        response = self.client.get(reverse("courses:enrol", args=[self.course.pk]))
        self.assertEqual(response.status_code, 405)


class LearnerLessonTests(LearnerFlowTestCase):
    """Lesson access requires enrolment; content is escaped and line-broken."""

    def test_draft_course_lesson_is_hidden(self) -> None:
        draft = make_course(self.instructor, "Hidden")
        lesson = make_lesson(draft, "Secret")
        response = self.client.get(
            reverse("courses:learner_lesson_detail", args=[draft.pk, lesson.pk])
        )
        self.assertEqual(response.status_code, NOT_FOUND)

    def test_non_enrolled_learner_is_sent_to_the_course(self) -> None:
        response = self.client.get(self._lesson_url(self.lesson_one))
        self.assertRedirects(response, self._course_url())
        followed = self.client.get(self._lesson_url(self.lesson_one), follow=True)
        self.assertContains(followed, "Enrol in this course")

    def test_enrolled_learner_sees_escaped_content(self) -> None:
        self._enrol()
        self.lesson_one.content = "Line one\nLine two <script>alert(1)</script>"
        self.lesson_one.save()
        response = self.client.get(self._lesson_url(self.lesson_one))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<br>", html=False)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_shows_video_and_attachment_links(self) -> None:
        self._enrol()
        self.lesson_one.video_url = "https://example.com/v"
        self.lesson_one.attachment = "course_attachments/notes.pdf"
        self.lesson_one.save()
        response = self.client.get(self._lesson_url(self.lesson_one))
        self.assertContains(response, "https://example.com/v")
        self.assertContains(
            response,
            reverse(
                "courses:lesson_attachment",
                args=[self.course.pk, self.lesson_one.pk],
            ),
        )
        self.assertNotContains(response, self.lesson_one.attachment.url)

    def test_previous_and_next_navigation(self) -> None:
        self._enrol()
        response = self.client.get(self._lesson_url(self.lesson_two))
        self.assertContains(response, self._lesson_url(self.lesson_one))  # previous


class LessonCompleteTests(LearnerFlowTestCase):
    """Marking a lesson complete is POST-only and idempotent."""

    def test_marks_complete_and_advances_to_the_next_lesson(self) -> None:
        enrollment = self._enrol()
        response = self.client.post(
            reverse(
                "courses:lesson_complete", args=[self.course.pk, self.lesson_one.pk]
            )
        )
        self.assertRedirects(response, self._lesson_url(self.lesson_two))
        self.assertTrue(
            LessonProgress.objects.filter(
                enrollment=enrollment, lesson=self.lesson_one
            ).exists()
        )

    def test_marking_twice_counts_once(self) -> None:
        enrollment = self._enrol()
        url = reverse(
            "courses:lesson_complete", args=[self.course.pk, self.lesson_one.pk]
        )
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(
            LessonProgress.objects.filter(enrollment=enrollment).count(), 1
        )
        self.assertEqual(course_progress(enrollment)["completed"], 1)

    def test_last_lesson_returns_to_the_course(self) -> None:
        self._enrol()
        response = self.client.post(
            reverse(
                "courses:lesson_complete", args=[self.course.pk, self.lesson_two.pk]
            )
        )
        self.assertRedirects(response, self._course_url())

    def test_requires_enrolment(self) -> None:
        response = self.client.post(
            reverse(
                "courses:lesson_complete", args=[self.course.pk, self.lesson_one.pk]
            )
        )
        self.assertEqual(response.status_code, NOT_FOUND)
        self.assertFalse(LessonProgress.objects.exists())

    def test_rejects_get(self) -> None:
        self._enrol()
        response = self.client.get(
            reverse(
                "courses:lesson_complete", args=[self.course.pk, self.lesson_one.pk]
            )
        )
        self.assertEqual(response.status_code, 405)


class LessonOrderEnforcementTests(LearnerFlowTestCase):
    """``Course.enforce_order``: lessons open strictly in order."""

    def setUp(self) -> None:
        super().setUp()
        self.course.enforce_order = True
        self.course.save(update_fields=["enforce_order"])
        self.enrollment = self._enrol()

    def _complete_url(self, lesson: Lesson) -> str:
        return reverse("courses:lesson_complete", args=[self.course.pk, lesson.pk])

    def _complete(self, lesson: Lesson) -> None:
        LessonProgress.objects.create(enrollment=self.enrollment, lesson=lesson)

    def test_first_lesson_is_open(self) -> None:
        response = self.client.get(self._lesson_url(self.lesson_one))
        self.assertEqual(response.status_code, 200)

    def test_skipping_ahead_by_url_is_blocked(self) -> None:
        response = self.client.get(self._lesson_url(self.lesson_two))
        self.assertRedirects(response, self._course_url())
        followed = self.client.get(self._lesson_url(self.lesson_two), follow=True)
        self.assertContains(followed, self.lesson_one.title)

    def test_completing_a_locked_lesson_by_post_is_blocked(self) -> None:
        response = self.client.post(self._complete_url(self.lesson_two))
        self.assertRedirects(response, self._course_url())
        self.assertFalse(
            LessonProgress.objects.filter(
                enrollment=self.enrollment, lesson=self.lesson_two
            ).exists()
        )

    def test_completing_in_order_unlocks_the_next(self) -> None:
        self.client.post(self._complete_url(self.lesson_one))
        response = self.client.get(self._lesson_url(self.lesson_two))
        self.assertEqual(response.status_code, 200)

    def test_outline_shows_the_lock_and_its_reason(self) -> None:
        response = self.client.get(self._course_url())
        self.assertContains(response, "Locked")
        self.assertContains(
            response, f"Complete \u201c{self.lesson_one.title}\u201d first."
        )
        # The locked lesson is not linked.
        self.assertNotContains(response, self._lesson_url(self.lesson_two))

    def test_next_link_is_hidden_until_the_lesson_is_done(self) -> None:
        response = self.client.get(self._lesson_url(self.lesson_one))
        self.assertNotContains(response, self._lesson_url(self.lesson_two))
        self.assertContains(response, "unlock")

    def test_turning_enforcement_off_unlocks_everything(self) -> None:
        self.course.enforce_order = False
        self.course.save(update_fields=["enforce_order"])
        self.assertEqual(
            self.client.get(self._lesson_url(self.lesson_two)).status_code, 200
        )
        response = self.client.post(self._complete_url(self.lesson_two))
        self.assertRedirects(response, self._course_url())
        self.assertTrue(
            LessonProgress.objects.filter(
                enrollment=self.enrollment, lesson=self.lesson_two
            ).exists()
        )

    def test_reordering_lessons_does_not_break_access(self) -> None:
        self._complete(self.lesson_one)
        # Swap the two lessons: the completed one moves to position 2.
        move_lesson_up(self.lesson_two)
        self.lesson_one.refresh_from_db()
        self.lesson_two.refresh_from_db()
        self.assertEqual(self.lesson_two.position, 1)
        # The newly-first lesson is the first incomplete one, so it opens; the
        # completed lesson stays readable wherever it sits.
        self.assertEqual(
            self.client.get(self._lesson_url(self.lesson_two)).status_code, 200
        )
        self.assertEqual(
            self.client.get(self._lesson_url(self.lesson_one)).status_code, 200
        )

    def test_inserting_a_lesson_does_not_break_access(self) -> None:
        self._complete(self.lesson_one)
        inserted = make_lesson(self.course, "Inserted")  # position 3
        # Free the slot before assigning it, to respect the unique constraint.
        self.lesson_two.position = 99
        self.lesson_two.save(update_fields=["position"])
        inserted.position = 2
        inserted.save(update_fields=["position"])
        self.assertEqual(self.client.get(self._lesson_url(inserted)).status_code, 200)
        self.assertRedirects(
            self.client.get(self._lesson_url(self.lesson_two)), self._course_url()
        )
        self._complete(inserted)
        self.assertEqual(
            self.client.get(self._lesson_url(self.lesson_two)).status_code, 200
        )


class OrderServiceTests(TestCase):
    """The ordering rule as a pure function, away from the HTTP layer."""

    def setUp(self) -> None:
        self.instructor = make_instructor("teacher")
        self.learner = make_learner("student")
        self.course = make_course(self.instructor, "Evidence")
        self.course.enforce_order = True
        self.course.save(update_fields=["enforce_order"])
        self.one = make_lesson(self.course, "One")
        self.two = make_lesson(self.course, "Two")
        self.three = make_lesson(self.course, "Three")
        self.enrollment = enrol(self.learner, self.course)

    def _lessons(self) -> list[Lesson]:
        return list(self.course.lessons.all())

    def test_without_enforcement_every_lesson_is_open(self) -> None:
        self.course.enforce_order = False
        self.course.save(update_fields=["enforce_order"])
        self.assertEqual(
            unlocked_lesson_ids(self.enrollment),
            {self.one.pk, self.two.pk, self.three.pk},
        )

    def test_only_the_first_incomplete_lesson_is_open(self) -> None:
        self.assertEqual(unlocked_lesson_ids(self.enrollment), {self.one.pk})
        self.assertTrue(can_access_lesson(self.enrollment, self.one))
        self.assertFalse(can_access_lesson(self.enrollment, self.two))

    def test_prefix_plus_completed_lessons_are_open(self) -> None:
        mark_lesson_complete(self.enrollment, self.one)
        self.assertEqual(
            unlocked_lesson_ids(self.enrollment), {self.one.pk, self.two.pk}
        )
        # A later, already-completed lesson stays readable out of order.
        mark_lesson_complete(self.enrollment, self.three)
        self.assertEqual(
            unlocked_lesson_ids(self.enrollment),
            {self.one.pk, self.two.pk, self.three.pk},
        )

    def test_first_blocking_lesson_names_the_gate(self) -> None:
        self.assertEqual(first_blocking_lesson(self.enrollment, self.three), self.one)
        mark_lesson_complete(self.enrollment, self.one)
        self.assertEqual(first_blocking_lesson(self.enrollment, self.three), self.two)
        mark_lesson_complete(self.enrollment, self.two)
        self.assertIsNone(first_blocking_lesson(self.enrollment, self.three))


class PreviewModeTests(LearnerFlowTestCase):
    """The owning instructor may preview their own course and its lessons."""

    def test_owner_previews_a_published_lesson_without_enrolling(self) -> None:
        self.client.force_login(self.instructor)
        response = self.client.get(self._lesson_url(self.lesson_two))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_preview"])

    def test_owner_previews_a_draft_lesson(self) -> None:
        draft = make_course(self.instructor, "Draft")
        lesson = make_lesson(draft, "Hidden")
        self.client.force_login(self.instructor)
        response = self.client.get(
            reverse("courses:learner_lesson_detail", args=[draft.pk, lesson.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_preview_bypasses_ordering(self) -> None:
        self.course.enforce_order = True
        self.course.save(update_fields=["enforce_order"])
        self.client.force_login(self.instructor)
        self.assertEqual(
            self.client.get(self._lesson_url(self.lesson_two)).status_code, 200
        )

    def test_preview_does_not_record_progress(self) -> None:
        self.client.force_login(self.instructor)
        response = self.client.post(
            reverse(
                "courses:lesson_complete", args=[self.course.pk, self.lesson_one.pk]
            )
        )
        self.assertEqual(response.status_code, NOT_FOUND)
        self.assertFalse(LessonProgress.objects.exists())

    def test_preview_notice_is_shown(self) -> None:
        self.client.force_login(self.instructor)
        response = self.client.get(self._course_url())
        self.assertContains(response, "previewing this course")


def make_quiz_with_question(course: Course, pass_mark: int = 70) -> Quiz:
    """Create a one-question quiz for ``course`` and return it."""
    quiz = Quiz.objects.create(course=course, pass_mark=pass_mark)
    question = Question.objects.create(quiz=quiz, text="Q1", position=1)
    Choice.objects.create(question=question, text="Yes", is_correct=True)
    Choice.objects.create(question=question, text="No", is_correct=False)
    return quiz


def record_attempt(enrollment: Enrollment, quiz: Quiz, passed: bool) -> QuizAttempt:
    """Store a minimal immutable attempt with the given outcome."""
    return QuizAttempt.objects.create(
        enrollment=enrollment,
        quiz=quiz,
        score=1 if passed else 0,
        total=1,
        percent=Decimal("100.00") if passed else Decimal("0.00"),
        passed=passed,
    )


class CertificateServiceTests(TestCase):
    """Eligibility rules and the idempotent issuing service."""

    def setUp(self) -> None:
        self.instructor = make_instructor("teacher")
        self.learner = make_learner("student")
        self.course = publish(make_course(self.instructor, "Evidence"))
        self.lesson_one = make_lesson(self.course, "First")
        self.lesson_two = make_lesson(self.course, "Second")
        self.enrollment = enrol(self.learner, self.course)

    def _complete_all(self) -> None:
        for lesson in (self.lesson_one, self.lesson_two):
            mark_lesson_complete(self.enrollment, lesson)

    def test_not_eligible_until_every_lesson_is_done(self) -> None:
        self.assertFalse(certificate_eligible(self.enrollment))
        mark_lesson_complete(self.enrollment, self.lesson_one)
        self.assertFalse(certificate_eligible(self.enrollment))

    def test_eligible_with_no_quiz_once_lessons_done(self) -> None:
        self._complete_all()
        self.assertTrue(certificate_eligible(self.enrollment))

    def test_a_course_with_no_lessons_is_never_eligible(self) -> None:
        empty = publish(make_course(self.instructor, "Empty"))
        enrollment = enrol(self.learner, empty)
        self.assertFalse(certificate_eligible(enrollment))

    def test_quiz_must_be_passed(self) -> None:
        self._complete_all()
        quiz = make_quiz_with_question(self.course)
        self.assertFalse(certificate_eligible(self.enrollment))
        record_attempt(self.enrollment, quiz, passed=False)
        self.assertFalse(certificate_eligible(self.enrollment))
        record_attempt(self.enrollment, quiz, passed=True)
        self.assertTrue(certificate_eligible(self.enrollment))

    def test_quiz_without_questions_is_not_required(self) -> None:
        self._complete_all()
        Quiz.objects.create(course=self.course)
        self.assertTrue(certificate_eligible(self.enrollment))

    def test_issue_returns_none_until_eligible(self) -> None:
        self.assertIsNone(issue_certificate_if_eligible(self.enrollment))
        self.assertFalse(Certificate.objects.exists())

    def test_issue_creates_one_certificate_with_a_uuid(self) -> None:
        self._complete_all()
        certificate = issue_certificate_if_eligible(self.enrollment)
        self.assertIsNotNone(certificate)
        self.assertIsInstance(certificate.code, uuid.UUID)
        self.assertIsNotNone(certificate.issued_at)
        self.assertEqual(certificate.course, self.course)

    def test_issue_is_idempotent(self) -> None:
        self._complete_all()
        first = issue_certificate_if_eligible(self.enrollment)
        second = issue_certificate_if_eligible(self.enrollment)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Certificate.objects.count(), 1)

    def test_issued_certificate_survives_a_new_lesson(self) -> None:
        self._complete_all()
        certificate = issue_certificate_if_eligible(self.enrollment)
        make_lesson(self.course, "Later")
        # The enrolment is no longer eligible for a *new* certificate...
        self.assertFalse(certificate_eligible(self.enrollment))
        self.assertIsNone(issue_certificate_if_eligible(self.enrollment))
        # ...but the already-issued one is untouched and still the only row.
        certificate.refresh_from_db()
        self.assertEqual(Certificate.objects.count(), 1)
        self.assertEqual(Certificate.objects.get().pk, certificate.pk)

    def test_holder_name_falls_back_to_the_username(self) -> None:
        self._complete_all()
        certificate = issue_certificate_if_eligible(self.enrollment)
        self.assertEqual(certificate.holder_name, "student")
        self.learner.first_name = "Ada"
        self.learner.last_name = "Lovelace"
        self.learner.save()
        self.assertEqual(certificate.holder_name, "Ada Lovelace")


class CertificateFlowTestCase(LearnerFlowTestCase):
    """Shared setup: an enrolled learner who has finished every lesson."""

    def setUp(self) -> None:
        super().setUp()
        self.enrollment = self._enrol()
        mark_lesson_complete(self.enrollment, self.lesson_one)
        mark_lesson_complete(self.enrollment, self.lesson_two)

    def _issue_url(self) -> str:
        return reverse("courses:certificate_issue", args=[self.course.pk])

    def _certificate_url(self) -> str:
        return reverse("courses:certificate", args=[self.course.pk])


class CertificateViewTests(CertificateFlowTestCase):
    """The learner-facing button, issuing POST and printable page."""

    def test_button_shown_when_eligible(self) -> None:
        response = self.client.get(self._course_url())
        self.assertContains(response, "Download certificate")
        self.assertContains(response, self._issue_url())

    def test_button_hidden_when_not_eligible(self) -> None:
        self.enrollment.lesson_progress.all().delete()
        response = self.client.get(self._course_url())
        self.assertNotContains(response, "Download certificate")

    def test_issue_creates_the_certificate_and_redirects_to_print(self) -> None:
        response = self.client.post(self._issue_url())
        self.assertRedirects(response, self._certificate_url())
        self.assertTrue(Certificate.objects.filter(enrollment=self.enrollment).exists())

    def test_issue_requires_a_post(self) -> None:
        response = self.client.get(self._issue_url())
        self.assertEqual(response.status_code, 405)

    def test_printable_page_shows_the_certificate_facts(self) -> None:
        self.client.post(self._issue_url())
        certificate = Certificate.objects.get(enrollment=self.enrollment)
        response = self.client.get(self._certificate_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Certificate of completion")
        self.assertContains(response, self.learner.get_username())
        self.assertContains(response, self.course.title)
        self.assertContains(response, str(certificate.code))
        self.assertContains(
            response, reverse("certificate_verify", args=[certificate.code])
        )

    def test_issue_when_not_eligible_redirects_without_creating(self) -> None:
        self.enrollment.lesson_progress.all().delete()
        response = self.client.post(self._issue_url())
        self.assertRedirects(response, self._course_url())
        self.assertFalse(Certificate.objects.exists())

    def test_printable_page_without_a_certificate_is_not_found(self) -> None:
        response = self.client.get(self._certificate_url())
        self.assertEqual(response.status_code, NOT_FOUND)

    def test_an_issued_certificate_stays_downloadable_after_a_new_lesson(self) -> None:
        self.client.post(self._issue_url())
        make_lesson(self.course, "Later")
        self.assertEqual(self.client.get(self._certificate_url()).status_code, 200)
        # The button remains (the certificate exists even though the learner is
        # no longer eligible for a fresh one).
        self.assertContains(self.client.get(self._course_url()), "Download certificate")

    def test_another_learner_cannot_reach_the_certificate(self) -> None:
        outsider = make_learner("outsider")
        self.client.force_login(outsider)
        self.assertEqual(
            self.client.get(self._certificate_url()).status_code, NOT_FOUND
        )
        self.assertEqual(self.client.post(self._issue_url()).status_code, NOT_FOUND)

    def test_instructor_preview_cannot_issue_a_certificate(self) -> None:
        self.client.force_login(self.instructor)
        self.assertEqual(self.client.post(self._issue_url()).status_code, NOT_FOUND)
        self.assertFalse(Certificate.objects.exists())

    def test_certificate_requires_login(self) -> None:
        self.client.logout()
        self.assertRedirects(
            self.client.get(self._certificate_url()),
            f"{reverse('accounts:login')}?next={self._certificate_url()}",
        )


class CertificateVerifyTests(TestCase):
    """The public ``/certificates/<uuid>/`` verification page."""

    def setUp(self) -> None:
        self.instructor = make_instructor("teacher")
        self.learner = make_learner("student")
        self.learner.first_name = "Ada"
        self.learner.last_name = "Lovelace"
        self.learner.email = "secret@example.com"
        self.learner.save()
        self.course = publish(make_course(self.instructor, "Evidence"))
        self.lesson = make_lesson(self.course, "First")
        self.enrollment = enrol(self.learner, self.course)
        mark_lesson_complete(self.enrollment, self.lesson)
        self.certificate = issue_certificate_if_eligible(self.enrollment)

    def _url(self, code: object = None) -> str:
        return reverse("certificate_verify", args=[code or self.certificate.code])

    def test_no_login_is_required(self) -> None:
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_a_valid_code_reports_valid(self) -> None:
        response = self.client.get(self._url())
        self.assertContains(response, "Valid certificate")

    def test_shows_the_holder_and_course(self) -> None:
        response = self.client.get(self._url())
        self.assertContains(response, "Ada Lovelace")
        self.assertContains(response, self.course.title)

    def test_an_unknown_code_is_not_found(self) -> None:
        response = self.client.get(self._url(uuid.uuid4()))
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "No certificate", status_code=404)

    def test_does_not_expose_private_data(self) -> None:
        response = self.client.get(self._url())
        self.assertNotContains(response, "secret@example.com")
        # The username is not shown either; only the printed display name is.
        self.assertNotContains(response, "student")

    def test_a_non_uuid_code_does_not_match_a_route(self) -> None:
        response = self.client.get("/certificates/not-a-uuid/")
        self.assertEqual(response.status_code, 404)


class ProgressServiceTests(TestCase):
    """Pure service behaviour: idempotency and progress arithmetic."""

    def setUp(self) -> None:
        self.instructor = make_instructor("teacher")
        self.learner = make_learner("student")
        self.course = make_course(self.instructor, "Evidence")
        self.enrollment = enrol(self.learner, self.course)

    def test_enrol_is_idempotent(self) -> None:
        again = enrol(self.learner, self.course)
        self.assertEqual(Enrollment.objects.count(), 1)
        self.assertEqual(again.pk, self.enrollment.pk)

    def test_mark_lesson_complete_returns_true_then_false(self) -> None:
        lesson = make_lesson(self.course, "L")
        self.assertTrue(mark_lesson_complete(self.enrollment, lesson))
        self.assertFalse(mark_lesson_complete(self.enrollment, lesson))
        self.assertEqual(LessonProgress.objects.count(), 1)

    def test_zero_lesson_course_reports_zero_percent(self) -> None:
        self.assertEqual(
            course_progress(self.enrollment),
            {"completed": 0, "total": 0, "percent": 0},
        )

    def test_percent_rounds_to_whole_number(self) -> None:
        lessons = [make_lesson(self.course, f"L{i}") for i in range(7)]
        for lesson in lessons[:4]:
            mark_lesson_complete(self.enrollment, lesson)
        self.assertEqual(
            course_progress(self.enrollment),
            {"completed": 4, "total": 7, "percent": 57},
        )

    def test_percent_rounds_up_when_nearest(self) -> None:
        lessons = [make_lesson(self.course, f"L{i}") for i in range(3)]
        mark_lesson_complete(self.enrollment, lessons[0])
        mark_lesson_complete(self.enrollment, lessons[1])
        self.assertEqual(course_progress(self.enrollment)["percent"], 67)

    def test_full_completion_is_one_hundred(self) -> None:
        lesson = make_lesson(self.course, "L")
        mark_lesson_complete(self.enrollment, lesson)
        self.assertEqual(course_progress(self.enrollment)["percent"], 100)


class MyCoursesTests(LearnerFlowTestCase):
    """'My courses' lists enrolments with a progress figure."""

    def test_lists_enrolled_courses_with_progress_text(self) -> None:
        enrollment = self._enrol()
        LessonProgress.objects.create(enrollment=enrollment, lesson=self.lesson_one)
        response = self.client.get(reverse("courses:my_courses"))
        self.assertContains(response, self.course.title)
        self.assertContains(response, "1 of 2 lessons done (50%)")

    def test_excludes_courses_the_learner_is_not_enrolled_in(self) -> None:
        other = publish(make_course(self.instructor, "Other course"))
        response = self.client.get(reverse("courses:my_courses"))
        self.assertNotContains(response, other.title)

    def test_requires_login(self) -> None:
        self.client.logout()
        url = reverse("courses:my_courses")
        self.assertRedirects(
            self.client.get(url), f"{reverse('accounts:login')}?next={url}"
        )


class LearnerQueryCountTests(LearnerFlowTestCase):
    """The catalogue and 'My courses' must not issue one query per row."""

    def _count_queries(self, url: str) -> int:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def test_catalogue_query_count_is_constant(self) -> None:
        url = reverse("courses:catalogue")
        baseline = self._count_queries(url)
        with self.assertNumQueries(baseline):
            self.client.get(url)
        for index in range(5):
            course = publish(make_course(self.instructor, f"Extra {index}"))
            make_lesson(course, "L")
        with self.assertNumQueries(baseline):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_catalogue_query_count_is_flat_with_filters(self) -> None:
        url = reverse("courses:catalogue")
        params = {"q": "e", "instructor": self.instructor.pk, "sort": "title"}
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)
        baseline = len(captured)
        for index in range(5):
            course = publish(make_course(self.instructor, f"Evidence extra {index}"))
            make_lesson(course, "L")
        with self.assertNumQueries(baseline):
            response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)

    def test_my_courses_query_count_is_constant(self) -> None:
        self._enrol()
        url = reverse("courses:my_courses")
        baseline = self._count_queries(url)
        with self.assertNumQueries(baseline):
            self.client.get(url)
        for index in range(5):
            course = publish(make_course(self.instructor, f"Extra {index}"))
            make_lesson(course, "L")
            Enrollment.objects.create(user=self.learner, course=course)
        with self.assertNumQueries(baseline):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class UnpublishPolicyTests(LearnerFlowTestCase):
    """Withdrawing a published course: existing learners keep access.

    Unpublishing hides a course from the catalogue and blocks *new* enrolments,
    but a learner who enrolled while it was published keeps their history and
    can still open its lessons.
    """

    def setUp(self) -> None:
        super().setUp()
        self.enrollment = self._enrol()
        # Withdraw the course *after* the learner has enrolled.
        self.course.status = Course.Status.DRAFT
        self.course.save()

    def test_enrolled_learner_still_opens_the_course(self) -> None:
        response = self.client.get(self._course_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.lesson_one.title)

    def test_enrolled_learner_still_opens_a_lesson(self) -> None:
        response = self.client.get(self._lesson_url(self.lesson_one))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.lesson_one.title)

    def test_enrolled_learner_can_still_mark_complete(self) -> None:
        response = self.client.post(
            reverse(
                "courses:lesson_complete", args=[self.course.pk, self.lesson_one.pk]
            )
        )
        self.assertRedirects(response, self._lesson_url(self.lesson_two))
        self.assertTrue(
            LessonProgress.objects.filter(
                enrollment=self.enrollment, lesson=self.lesson_one
            ).exists()
        )

    def test_existing_progress_history_is_preserved(self) -> None:
        LessonProgress.objects.create(
            enrollment=self.enrollment, lesson=self.lesson_one
        )
        response = self.client.get(self._course_url())
        self.assertContains(response, "1 of 2 lessons done (50%)")

    def test_enrolled_course_still_appears_in_my_courses(self) -> None:
        response = self.client.get(reverse("courses:my_courses"))
        self.assertContains(response, self.course.title)

    def test_absent_from_catalogue_even_for_an_enrolled_learner(self) -> None:
        response = self.client.get(reverse("courses:catalogue"))
        self.assertNotContains(response, self.course.title)

    def test_not_enrolled_learner_gets_404(self) -> None:
        self.client.force_login(make_learner("outsider"))
        response = self.client.get(self._course_url())
        self.assertEqual(response.status_code, NOT_FOUND)

    def test_not_enrolled_learner_cannot_enrol(self) -> None:
        outsider = make_learner("outsider")
        self.client.force_login(outsider)
        response = self.client.post(reverse("courses:enrol", args=[self.course.pk]))
        self.assertEqual(response.status_code, NOT_FOUND)
        self.assertFalse(
            Enrollment.objects.filter(user=outsider, course=self.course).exists()
        )


class DashboardTestCase(TestCase):
    """Shared fixture for the instructor dashboard tests.

    One course with three lessons and a two-question quiz, so both the lesson
    and attempt joins are exercised.  Attempts go through the real
    ``submit_attempt`` service so the stored scores are the production ones.
    """

    def setUp(self) -> None:
        self.instructor = make_instructor("owner")
        self.rival = make_instructor("rival")
        self.course = publish(make_course(self.instructor, "Evidence"))
        self.lessons = [
            make_lesson(self.course, f"Lesson {number}") for number in range(1, 4)
        ]
        self.quiz = Quiz.objects.create(course=self.course, pass_mark=70)
        self.questions = [
            Question.objects.create(quiz=self.quiz, text=f"Q{number}", position=number)
            for number in range(1, 3)
        ]
        self.correct_choices: dict[int, Choice] = {}
        self.wrong_choices: dict[int, Choice] = {}
        for question in self.questions:
            self.correct_choices[question.pk] = Choice.objects.create(
                question=question, text="Right", is_correct=True
            )
            self.wrong_choices[question.pk] = Choice.objects.create(
                question=question, text="Wrong"
            )
        self.client.force_login(self.instructor)

    def add_learner(self, username: str, completed: int = 0) -> Enrollment:
        """Enrol a fresh learner and mark ``completed`` lessons done."""
        enrollment = Enrollment.objects.create(
            user=make_learner(username), course=self.course
        )
        for lesson in self.lessons[:completed]:
            LessonProgress.objects.create(enrollment=enrollment, lesson=lesson)
        return enrollment

    def submit(self, enrollment: Enrollment, correct: list[bool]) -> QuizAttempt:
        """Submit one attempt, choosing right/wrong for each question in turn."""
        payload = {
            f"question_{question.pk}": str(
                (self.correct_choices if is_correct else self.wrong_choices)[
                    question.pk
                ].pk
            )
            for question, is_correct in zip(self.questions, correct, strict=True)
        }
        return submit_attempt(enrollment, self.quiz, payload)

    def rows_by_username(self) -> dict[str, LearnerStat]:
        return {row.username: row for row in course_learner_stats(self.course)}

    def dashboard_url(self) -> str:
        return reverse("courses:course_dashboard", args=[self.course.pk])

    def csv_url(self) -> str:
        return reverse("courses:course_dashboard_csv", args=[self.course.pk])


class CourseLearnerStatsTests(DashboardTestCase):
    """The service rows match a hand-computed fixture."""

    def test_counts_match_hand_computed_figures(self) -> None:
        ana = self.add_learner("ana", completed=3)
        self.submit(ana, [True, False])  # 50% - below the 70% pass mark
        self.submit(ana, [True, True])  # 100% - pass
        ben = self.add_learner("ben", completed=1)
        self.submit(ben, [False, False])  # 0% - fail
        self.add_learner("cara", completed=0)  # no attempts

        rows = self.rows_by_username()

        ana_row = rows["ana"]
        self.assertEqual(ana_row.lessons_completed, 3)
        self.assertEqual(ana_row.total_lessons, 3)
        self.assertEqual(ana_row.percent, 100)
        self.assertEqual(ana_row.best_quiz_percent, Decimal("100.00"))
        self.assertTrue(ana_row.passed)
        self.assertEqual(ana_row.attempts_count, 2)

        ben_row = rows["ben"]
        self.assertEqual(ben_row.lessons_completed, 1)
        self.assertEqual(ben_row.total_lessons, 3)
        self.assertEqual(ben_row.percent, 33)
        self.assertEqual(ben_row.best_quiz_percent, Decimal("0.00"))
        self.assertFalse(ben_row.passed)
        self.assertEqual(ben_row.attempts_count, 1)

        cara_row = rows["cara"]
        self.assertEqual(cara_row.lessons_completed, 0)
        self.assertEqual(cara_row.percent, 0)
        self.assertIsNone(cara_row.best_quiz_percent)
        self.assertFalse(cara_row.passed)
        self.assertEqual(cara_row.attempts_count, 0)

    def test_many_lessons_and_attempts_do_not_inflate_the_counts(self) -> None:
        # Without distinct=True the raw join would report 3 lessons x 2 attempts
        # = 6 for both lessons_completed and attempts_count.
        ana = self.add_learner("ana", completed=3)
        self.submit(ana, [True, True])
        self.submit(ana, [True, False])

        row = course_learner_stats(self.course)[0]

        self.assertEqual(row.lessons_completed, 3)
        self.assertEqual(row.total_lessons, 3)
        self.assertEqual(row.attempts_count, 2)
        self.assertEqual(row.best_quiz_percent, Decimal("100.00"))

    def test_rows_are_ordered_by_username(self) -> None:
        for username in ("cara", "ana", "ben"):
            self.add_learner(username)
        usernames = [row.username for row in course_learner_stats(self.course)]
        self.assertEqual(usernames, ["ana", "ben", "cara"])

    def test_course_without_quiz_has_no_analytics(self) -> None:
        other = make_course(self.instructor, "No quiz")
        self.assertEqual(course_question_stats(other), [])


class CourseLearnerStatsQueryTests(DashboardTestCase):
    """Aggregating the learner table stays one query however many learners."""

    def test_query_count_is_one_regardless_of_learner_count(self) -> None:
        self.add_learner("ana", completed=2)
        with self.assertNumQueries(1):
            course_learner_stats(self.course)

        for index in range(5):
            enrollment = self.add_learner(f"extra{index}", completed=3)
            self.submit(enrollment, [True, True])
            self.submit(enrollment, [False, True])

        with self.assertNumQueries(1):
            rows = course_learner_stats(self.course)
        self.assertEqual(len(rows), 6)


class CourseQuestionStatsTests(DashboardTestCase):
    """Per-question percent-correct analytics across several attempts."""

    def test_percent_correct_over_multiple_attempts(self) -> None:
        ana = self.add_learner("ana")
        self.submit(ana, [True, False])  # Q1 right, Q2 wrong
        self.submit(ana, [True, True])  # Q1 right, Q2 right
        ben = self.add_learner("ben")
        self.submit(ben, [False, False])  # both wrong

        rows = course_question_stats(self.course)

        self.assertEqual([row.question.text for row in rows], ["Q1", "Q2"])
        self.assertEqual([row.answers_count for row in rows], [3, 3])
        self.assertEqual([row.correct_count for row in rows], [2, 1])
        self.assertEqual([row.percent for row in rows], [67, 33])


class InstructorDashboardIndexTests(DashboardTestCase):
    """The index lists the instructor's own courses with learner counts."""

    def test_lists_own_courses_with_counts(self) -> None:
        self.add_learner("ana")
        self.add_learner("ben")
        rival_course = make_course(self.rival, "Rival course")

        response = self.client.get(reverse("courses:instructor_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.course.title)
        self.assertNotContains(response, rival_course.title)
        listed = {course.pk: course for course in response.context["courses"]}
        self.assertEqual(listed[self.course.pk].learner_count, 2)
        self.assertEqual(listed[self.course.pk].lesson_count, 3)

    def test_learner_is_forbidden(self) -> None:
        self.client.force_login(make_learner("outsider"))
        response = self.client.get(reverse("courses:instructor_dashboard"))
        self.assertEqual(response.status_code, FORBIDDEN)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.client.logout()
        url = reverse("courses:instructor_dashboard")
        self.assertRedirects(
            self.client.get(url), f"{reverse('accounts:login')}?next={url}"
        )


class CourseDashboardViewTests(DashboardTestCase):
    """The per-course page renders the learner table, summary and analytics."""

    def test_renders_learner_rows_and_summary(self) -> None:
        ana = self.add_learner("ana", completed=3)
        self.submit(ana, [True, True])
        self.add_learner("ben", completed=1)

        response = self.client.get(self.dashboard_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ana")
        self.assertContains(response, "ben")
        self.assertEqual(response.context["enrolled_count"], 2)
        self.assertEqual(response.context["completed_count"], 1)
        self.assertEqual(response.context["passed_count"], 1)

    def test_renders_question_analytics(self) -> None:
        ana = self.add_learner("ana")
        self.submit(ana, [True, False])

        response = self.client.get(self.dashboard_url())

        percents = [row.percent for row in response.context["question_rows"]]
        self.assertEqual(percents, [100, 0])

    def test_empty_course_shows_a_message(self) -> None:
        response = self.client.get(self.dashboard_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No learners have enrolled yet.")


class CourseDashboardQueryCountTests(DashboardTestCase):
    """The dashboard page stays a constant number of queries as learners grow."""

    def _count_queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self.dashboard_url())
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def test_query_count_is_constant(self) -> None:
        self.add_learner("ana", completed=1)
        baseline = self._count_queries()

        for index in range(5):
            enrollment = self.add_learner(f"extra{index}", completed=3)
            self.submit(enrollment, [True, True])
            self.submit(enrollment, [False, True])

        with self.assertNumQueries(baseline):
            response = self.client.get(self.dashboard_url())
        self.assertEqual(response.status_code, 200)


class CourseDashboardAccessTests(DashboardTestCase):
    """Only the owning instructor may see the dashboard or its CSV export."""

    def test_other_instructor_gets_404(self) -> None:
        self.client.force_login(self.rival)
        self.assertEqual(self.client.get(self.dashboard_url()).status_code, NOT_FOUND)
        self.assertEqual(self.client.get(self.csv_url()).status_code, NOT_FOUND)

    def test_learner_is_forbidden(self) -> None:
        self.client.force_login(make_learner("outsider"))
        self.assertEqual(self.client.get(self.dashboard_url()).status_code, FORBIDDEN)
        self.assertEqual(self.client.get(self.csv_url()).status_code, FORBIDDEN)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.client.logout()
        self.assertRedirects(
            self.client.get(self.dashboard_url()),
            f"{reverse('accounts:login')}?next={self.dashboard_url()}",
        )


class CourseDashboardCsvTests(DashboardTestCase):
    """The CSV export mirrors the table and is safe to open in a spreadsheet."""

    def _rows(self, response: object) -> list[list[str]]:
        return list(csv.reader(response.content.decode().splitlines()))

    def test_headers_and_learner_rows(self) -> None:
        ana = self.add_learner("ana", completed=3)
        self.submit(ana, [True, True])

        response = self.client.get(self.csv_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment", response["Content-Disposition"])
        rows = self._rows(response)
        self.assertEqual(rows[0][0], "Username")
        self.assertEqual(rows[1][0], "ana")
        # ana: enrolled at (dynamic), 3/3 lessons, 100%, best 100.00, passed, 1 try.
        self.assertEqual(rows[1][2:], ["3", "3", "100", "100.00", "Yes", "1"])
        self.assertTrue(rows[1][1])

    def test_malicious_username_is_neutralised(self) -> None:
        username = "=cmd|' /C calc'!A0"
        enrollment = Enrollment.objects.create(
            user=make_learner(username), course=self.course
        )
        self.assertIsNotNone(enrollment.pk)

        response = self.client.get(self.csv_url())

        cells = [row[0] for row in self._rows(response)[1:]]
        self.assertEqual(cells, ["'" + username])
        self.assertTrue(all(not cell.startswith("=") for cell in cells))

    def test_username_hidden_behind_leading_whitespace_is_neutralised(self) -> None:
        # A spreadsheet strips leading whitespace/control characters before
        # deciding a cell is a formula, so "\t=..." is as dangerous as "=...".
        username = "\t=cmd|' /C calc'!A0"
        Enrollment.objects.create(user=make_learner(username), course=self.course)

        cells = [row[0] for row in self._rows(self.client.get(self.csv_url()))[1:]]

        self.assertEqual(cells, ["'" + username])

    def test_numeric_columns_are_not_escaped(self) -> None:
        ana = self.add_learner("ana", completed=3)
        self.submit(ana, [True, True])

        row = self._rows(self.client.get(self.csv_url()))[1]

        self.assertEqual(row[2:], ["3", "3", "100", "100.00", "Yes", "1"])
        self.assertTrue(all(not cell.startswith("'") for cell in row[2:]))

    def test_csv_matches_the_learner_stats_service(self) -> None:
        ana = self.add_learner("ana", completed=3)
        self.submit(ana, [True, True])
        self.submit(ana, [False, False])
        self.add_learner("ben", completed=1)

        stats = {row.username: row for row in course_learner_stats(self.course)}
        exported = {
            row[0]: row for row in self._rows(self.client.get(self.csv_url()))[1:]
        }

        self.assertEqual(set(exported), set(stats))
        for username, stat in stats.items():
            row = exported[username]
            best = (
                ""
                if stat.best_quiz_percent is None
                else f"{stat.best_quiz_percent:.2f}"
            )
            self.assertEqual(
                row[2:],
                [
                    str(stat.lessons_completed),
                    str(stat.total_lessons),
                    str(stat.percent),
                    best,
                    "Yes" if stat.passed else "No",
                    str(stat.attempts_count),
                ],
            )


class CsvSafeTests(TestCase):
    """``_csv_safe`` prefixes cells a spreadsheet would treat as formulas."""

    def test_prefixes_each_dangerous_leading_character(self) -> None:
        for value in ("=1+1", "+1", "-1", "@SUM(A1)"):
            with self.subTest(value=value):
                self.assertEqual(_csv_safe(value), "'" + value)

    def test_prefixes_after_leading_whitespace_or_control_characters(self) -> None:
        for value in (
            " =1+1",
            "\t=1+1",
            "\r=1",
            "\n@SUM(A1)",
            "\x00=cmd",
            "\x0b+1",
            "\u00a0-1",
            "\ufeff@x",
        ):
            with self.subTest(value=value):
                self.assertEqual(_csv_safe(value), "'" + value)

    def test_leaves_ordinary_values_unchanged(self) -> None:
        self.assertEqual(_csv_safe("ana"), "ana")
        self.assertEqual(_csv_safe(3), "3")
        self.assertEqual(_csv_safe(None), "")
        self.assertEqual(_csv_safe("100.00"), "100.00")


class LessonAttachmentTests(TestCase):
    """Lesson attachments are private: streamed only to owner or enrolled.

    Files are written to a throw-away MEDIA_ROOT so the repository is not
    polluted and the tests are independent of the developer's media folder.
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self) -> None:
        self.owner = make_instructor("owner")
        self.rival = make_instructor("rival")
        self.learner = make_learner("student")
        self.outsider = make_learner("outsider")
        self.course = publish(make_course(self.owner, "Evidence"))
        self.lesson = make_lesson(self.course, "First")
        Enrollment.objects.create(user=self.learner, course=self.course)
        self.payload = b"%PDF-1.4 hello"
        self.lesson.attachment.save(
            "notes.pdf", SimpleUploadedFile("notes.pdf", self.payload), save=True
        )

    def url(self, lesson: Lesson | None = None) -> str:
        target = lesson or self.lesson
        return reverse("courses:lesson_attachment", args=[self.course.pk, target.pk])

    def test_enrolled_learner_downloads_the_attachment(self) -> None:
        self.client.force_login(self.learner)

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        disposition = response["Content-Disposition"]
        stored_name = self.lesson.attachment.name.rsplit("/", 1)[-1]
        self.assertTrue(disposition.startswith("attachment"))
        self.assertIn(f'filename="{stored_name}"', disposition)
        self.assertEqual(b"".join(response.streaming_content), self.payload)

    def test_owner_downloads_their_own_attachment(self) -> None:
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_non_enrolled_learner_is_denied(self) -> None:
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url()).status_code, FORBIDDEN)

    def test_another_instructor_is_denied_on_a_published_course(self) -> None:
        self.client.force_login(self.rival)
        self.assertEqual(self.client.get(self.url()).status_code, FORBIDDEN)

    def test_draft_attachment_is_404_for_an_enrolled_learner(self) -> None:
        self.course.status = Course.Status.DRAFT
        self.course.save()
        self.client.force_login(self.learner)
        self.assertEqual(self.client.get(self.url()).status_code, NOT_FOUND)

    def test_draft_attachment_is_404_for_another_instructor(self) -> None:
        self.course.status = Course.Status.DRAFT
        self.course.save()
        self.client.force_login(self.rival)
        self.assertEqual(self.client.get(self.url()).status_code, NOT_FOUND)

    def test_owner_can_still_download_a_draft_attachment(self) -> None:
        self.course.status = Course.Status.DRAFT
        self.course.save()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.client.logout()
        self.assertRedirects(
            self.client.get(self.url()),
            f"{reverse('accounts:login')}?next={self.url()}",
        )

    def test_lesson_without_attachment_is_404(self) -> None:
        other = make_lesson(self.course, "Second")
        self.client.force_login(self.learner)
        self.assertEqual(self.client.get(self.url(other)).status_code, NOT_FOUND)

    def test_disallowed_extension_is_not_served(self) -> None:
        # The content-type allowlist refuses anything it cannot vouch for, even
        # if a disallowed file somehow reached storage.
        self.lesson.attachment.name = "course_attachments/evil.exe"
        self.lesson.save(update_fields=["attachment"])
        self.client.force_login(self.learner)
        self.assertEqual(self.client.get(self.url()).status_code, NOT_FOUND)

    def test_missing_file_on_disk_is_404(self) -> None:
        self.lesson.attachment.storage.delete(self.lesson.attachment.name)
        self.client.force_login(self.learner)
        self.assertEqual(self.client.get(self.url()).status_code, NOT_FOUND)

    def test_lesson_page_links_to_the_view_not_the_media_url(self) -> None:
        self.client.force_login(self.learner)
        response = self.client.get(
            reverse(
                "courses:learner_lesson_detail",
                args=[self.course.pk, self.lesson.pk],
            )
        )
        self.assertContains(response, self.url())
        self.assertNotContains(response, self.lesson.attachment.url)

    def test_media_url_is_not_served_publicly(self) -> None:
        media_url = f"/{settings.MEDIA_URL}{self.lesson.attachment.name}"
        self.client.force_login(self.learner)
        self.assertEqual(self.client.get(media_url).status_code, NOT_FOUND)


class LessonAttachmentOrderTests(TestCase):
    """A locked lesson's attachment is not downloadable (regression for M-2).

    The download view used to check only ownership/enrolment, so an enrolled
    learner could fetch every later lesson's file straight from its direct URL
    while the lesson page itself refused to open.  Files go to a throw-away
    MEDIA_ROOT, as in ``LessonAttachmentTests``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self) -> None:
        self.owner = make_instructor("owner")
        self.rival = make_instructor("rival")
        self.learner = make_learner("student")
        self.outsider = make_learner("outsider")
        self.course = publish(make_course(self.owner, "Evidence"))
        self.course.enforce_order = True
        self.course.save(update_fields=["enforce_order"])
        self.first = make_lesson(self.course, "First")
        self.second = make_lesson(self.course, "Second")
        self.enrollment = enrol(self.learner, self.course)
        self.payload = b"%PDF-1.4 second"
        self.second.attachment.save(
            "second.pdf", SimpleUploadedFile("second.pdf", self.payload), save=True
        )

    def url(self, lesson: Lesson | None = None) -> str:
        target = lesson or self.second
        return reverse("courses:lesson_attachment", args=[self.course.pk, target.pk])

    def _course_url(self) -> str:
        return reverse("courses:learner_course_detail", args=[self.course.pk])

    def test_locked_lesson_attachment_is_not_streamed_by_direct_url(self) -> None:
        self.client.force_login(self.learner)

        response = self.client.get(self.url())

        # Same treatment as the lesson page: back to the course with a warning.
        self.assertRedirects(response, self._course_url())
        followed = self.client.get(self.url(), follow=True)
        self.assertContains(followed, "First")
        self.assertNotIn("Content-Disposition", followed.headers)
        self.assertFalse(hasattr(response, "streaming_content"))

    def test_completing_the_previous_lesson_unlocks_the_attachment(self) -> None:
        LessonProgress.objects.create(enrollment=self.enrollment, lesson=self.first)
        self.client.force_login(self.learner)

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.payload)

    def test_ordering_off_streams_the_attachment_immediately(self) -> None:
        self.course.enforce_order = False
        self.course.save(update_fields=["enforce_order"])
        self.client.force_login(self.learner)

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.payload)

    def test_owner_preview_is_exempt_from_ordering(self) -> None:
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_non_enrolled_learner_is_still_403_not_redirected(self) -> None:
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url()).status_code, FORBIDDEN)

    def test_another_instructor_is_still_403(self) -> None:
        self.client.force_login(self.rival)
        self.assertEqual(self.client.get(self.url()).status_code, FORBIDDEN)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class SeedDemoCommandTests(TestCase):
    """seed_demo builds the demo dataset and does not duplicate it on re-run."""

    def run_seed(self) -> str:
        output = StringIO()
        call_command("seed_demo", stdout=output)
        return output.getvalue()

    def test_creates_accounts_courses_progress_and_an_attempt(self) -> None:
        output = self.run_seed()

        self.assertIn("demo_instructor", output)
        self.assertIn("demo_learner1", output)
        self.assertIn("demo_learner2", output)
        self.assertIn("demo12345", output)

        instructor = User.objects.get(username="demo_instructor")
        self.assertTrue(instructor.is_instructor)
        self.assertTrue(User.objects.get(username="demo_learner1").is_learner)
        self.assertTrue(User.objects.get(username="demo_learner2").is_learner)

        published = Course.objects.get(title="E-Filing for Court Clerks")
        self.assertTrue(published.is_published)
        self.assertEqual(published.lessons.count(), 5)
        self.assertEqual(published.quiz.questions.count(), 5)

        draft = Course.objects.get(title="Advanced E-Filing: Case Management")
        self.assertFalse(draft.is_published)

        learner1 = User.objects.get(username="demo_learner1")
        self.assertEqual(
            LessonProgress.objects.filter(enrollment__user=learner1).count(), 2
        )
        attempt = QuizAttempt.objects.get(enrollment__user=learner1)
        self.assertTrue(attempt.passed)
        self.assertFalse(
            QuizAttempt.objects.filter(
                enrollment__user__username="demo_learner2"
            ).exists()
        )

    def test_the_printed_password_works(self) -> None:
        self.run_seed()
        self.assertIsNotNone(
            authenticate(username="demo_learner1", password="demo12345")
        )

    def test_running_twice_creates_nothing_new(self) -> None:
        self.run_seed()

        def snapshot() -> tuple[int, ...]:
            return (
                User.objects.count(),
                Course.objects.count(),
                Lesson.objects.count(),
                Question.objects.count(),
                Choice.objects.count(),
                Enrollment.objects.count(),
                LessonProgress.objects.count(),
                QuizAttempt.objects.count(),
            )

        before = snapshot()
        self.run_seed()
        self.assertEqual(snapshot(), before)


class AccessibilityAuditTests(LearnerFlowTestCase):
    """Regression tests for the concrete fixes from the Task 15 audit."""

    def test_skip_link_target_is_focusable(self) -> None:
        response = self.client.get(reverse("courses:my_courses"))
        self.assertContains(response, 'href="#main-content"')
        self.assertContains(response, 'id="main-content"')
        self.assertContains(response, 'tabindex="-1"')

    def test_catalogue_filters_are_a_search_landmark(self) -> None:
        response = self.client.get(reverse("courses:catalogue"))
        self.assertContains(response, 'role="search"')

    def test_progress_bars_have_distinct_labels(self) -> None:
        self._enrol()
        other = publish(make_course(self.instructor, "Second course"))
        make_lesson(other, "Seed")
        enrol(self.learner, other)
        response = self.client.get(reverse("courses:my_courses"))
        self.assertContains(response, f'aria-label="{self.course.title}"')
        self.assertContains(response, f'aria-label="{other.title}"')

    def test_quiz_legend_contains_the_question_text(self) -> None:
        make_quiz_with_question(self.course)
        self._enrol()
        response = self.client.get(reverse("quizzes:take", args=[self.course.pk]))
        content = response.content.decode()
        legend = content[content.index("<legend") : content.index("</legend>")]
        self.assertIn("Question 1", legend)
        self.assertIn("Q1", legend)

    def test_instructor_action_links_name_their_course(self) -> None:
        self.client.force_login(self.instructor)
        response = self.client.get(reverse("courses:instructor_course_list"))
        content = response.content.decode()
        hidden_title = f'<span class="visually-hidden"> {self.course.title}</span>'
        # The Edit link and the Publish button each get the hidden course name.
        self.assertEqual(content.count(hidden_title), 2)

    def test_video_link_notes_the_new_tab(self) -> None:
        lesson = make_lesson(
            self.course, "With video", video_url="https://example.com/v"
        )
        self._enrol()
        response = self.client.get(self._lesson_url(lesson))
        self.assertContains(response, "opens in a new tab")
