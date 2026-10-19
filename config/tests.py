"""Project-level integration tests.

Cross-app on purpose: the admin registration itself (changelist querysets,
list_display callables, read-only rules), the site-wide language switcher, and
the settings bootstrap (SECRET_KEY is required, with no fallback key), none of
which belongs to a single app.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from courses.models import Course, Enrollment, Lesson, LessonProgress
from quizzes.models import AttemptAnswer, Choice, Question, Quiz, QuizAttempt

User = get_user_model()

NOT_FOUND = 404


class AdminChangelistTests(TestCase):
    """Every registered changelist renders for a superuser."""

    def setUp(self) -> None:
        self.admin_user = User.objects.create_superuser(username="root")
        self.client.force_login(self.admin_user)
        self._seed_one_of_each()

    def _seed_one_of_each(self) -> None:
        """Create one row per model so list_display callables really run."""
        instructor = User.objects.create_user(
            username="teacher", role=User.Role.INSTRUCTOR
        )
        learner = User.objects.create_user(username="learner")
        course = Course.objects.create(
            instructor=instructor,
            title="Law 101",
            status=Course.Status.PUBLISHED,
        )
        lesson = Lesson.objects.create(course=course, title="Intro", content="x")
        enrollment = Enrollment.objects.create(user=learner, course=course)
        LessonProgress.objects.create(enrollment=enrollment, lesson=lesson)
        quiz = Quiz.objects.create(course=course)
        question = Question.objects.create(quiz=quiz, text="Q?", position=1)
        choice = Choice.objects.create(question=question, text="A", is_correct=True)
        attempt = QuizAttempt.objects.create(
            enrollment=enrollment,
            quiz=quiz,
            score=1,
            total=1,
            percent=Decimal("100.00"),
            passed=True,
        )
        AttemptAnswer.objects.create(
            attempt=attempt, question=question, selected_choice=choice
        )

    def test_all_admin_changelists_load(self) -> None:
        for model in admin.site._registry:
            url = reverse(
                f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist"
            )
            with self.subTest(model=model._meta.label):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_attempt_admins_are_read_only(self) -> None:
        request = RequestFactory().get("/")
        request.user = self.admin_user
        for model in (QuizAttempt, AttemptAnswer):
            model_admin = admin.site._registry[model]
            with self.subTest(model=model.__name__):
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))

    def test_read_only_change_pages_load(self) -> None:
        for obj in (QuizAttempt.objects.first(), AttemptAnswer.objects.first()):
            model = type(obj)
            url = reverse(
                f"admin:{model._meta.app_label}_{model._meta.model_name}_change",
                args=[obj.pk],
            )
            with self.subTest(model=model.__name__):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_course_and_question_change_pages_load(self) -> None:
        for model, obj in (
            (Course, Course.objects.first()),
            (Question, Question.objects.first()),
        ):
            url = reverse(
                f"admin:{model._meta.app_label}_{model._meta.model_name}_change",
                args=[obj.pk],
            )
            with self.subTest(model=model.__name__):
                self.assertEqual(self.client.get(url).status_code, 200)


class AdminQueryCountTests(TestCase):
    """Changelists must not issue one query per row (N+1)."""

    def setUp(self) -> None:
        self.admin_user = User.objects.create_superuser(username="root")
        self.client.force_login(self.admin_user)
        instructor = User.objects.create_user(
            username="teacher", role=User.Role.INSTRUCTOR
        )
        self.course = Course.objects.create(instructor=instructor, title="Law 101")
        self.quiz = Quiz.objects.create(course=self.course)

    def _count_changelist_queries(self, url: str) -> int:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def test_course_changelist_queries_constant_with_more_rows(self) -> None:
        url = reverse("admin:courses_course_changelist")
        baseline = self._count_changelist_queries(url)
        for index in range(5):
            Course.objects.create(
                instructor=User.objects.get(username="teacher"),
                title=f"Extra {index}",
            )
        self.assertEqual(self._count_changelist_queries(url), baseline)

    def test_quiz_attempt_changelist_queries_constant_with_more_rows(self) -> None:
        url = reverse("admin:quizzes_quizattempt_changelist")
        baseline = self._count_changelist_queries(url)
        for index in range(5):
            learner = User.objects.create_user(username=f"extra{index}")
            enrollment = Enrollment.objects.create(user=learner, course=self.course)
            QuizAttempt.objects.create(
                enrollment=enrollment,
                quiz=self.quiz,
                score=1,
                total=1,
                percent=Decimal("100.00"),
                passed=True,
            )
        self.assertEqual(self._count_changelist_queries(url), baseline)


class MalayalamLanguageTests(TestCase):
    """English is the default; the nav switcher moves a visitor to Malayalam.

    These are the end-to-end check that locale/ml really compiled: a request
    with an ``ml`` Accept-Language header (or the cookie the switcher sets)
    must render the Malayalam catalogues, including plurals.
    """

    def test_pages_default_to_english(self) -> None:
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, '<html lang="en">', html=False)

    def test_login_page_renders_malayalam_from_accept_language(self) -> None:
        response = self.client.get(
            reverse("accounts:login"), HTTP_ACCEPT_LANGUAGE="ml-IN,ml;q=0.9"
        )
        self.assertContains(response, '<html lang="ml">', html=False)
        self.assertContains(response, "ലോഗിൻ ചെയ്യുക")

    def test_switcher_form_posts_to_set_language_with_next(self) -> None:
        response = self.client.get("/no-such-page/")
        self.assertContains(
            response, reverse("set_language"), status_code=NOT_FOUND, html=False
        )
        self.assertContains(
            response, 'method="post"', status_code=NOT_FOUND, html=False
        )
        self.assertContains(
            response, "csrfmiddlewaretoken", status_code=NOT_FOUND, html=False
        )

    def test_posting_the_switcher_sets_the_cookie_and_renders_ml(self) -> None:
        response = self.client.post(
            reverse("set_language"),
            {"language": "ml", "next": reverse("accounts:login")},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.cookies["django_language"].value,
            "ml",
        )
        follow_up = self.client.get(reverse("accounts:login"))
        self.assertContains(follow_up, '<html lang="ml">', html=False)
        self.assertContains(follow_up, "ലോഗിൻ ചെയ്യുക")
        # The switcher marks the now-active language as selected.
        self.assertContains(follow_up, '<option value="ml" selected>', html=False)
        # Django renders language names in the active language too.
        self.assertContains(follow_up, ">ഇംഗ്ലീഷ്</option>", html=False)

    def test_catalogue_lesson_counts_use_malayalam_plurals(self) -> None:
        instructor = User.objects.create_user(
            username="teacher", role=User.Role.INSTRUCTOR
        )
        for title, lesson_count in (("One", 1), ("Two", 2)):
            course = Course.objects.create(
                instructor=instructor,
                title=title,
                status=Course.Status.PUBLISHED,
            )
            for position in range(lesson_count):
                Lesson.objects.create(
                    course=course, title=f"{title} {position}", content="x"
                )

        learner = User.objects.create_user(username="reader")
        self.client.force_login(learner)
        self.client.post(reverse("set_language"), {"language": "ml", "next": "/"})
        response = self.client.get(reverse("courses:catalogue"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 പാഠം")
        self.assertContains(response, "2 പാഠങ്ങൾ")


class SecretKeyRequiredTests(TestCase):
    """``settings.py`` refuses to load without a ``SECRET_KEY`` (regression).

    Finding H-2: the module used to fall back to a key shipped in the
    repository, so a deployment that forgot the variable would happily sign
    sessions with a publicly known value.  Each check imports the real settings
    module in a subprocess from a scratch package directory, which means the
    developer's own ``.env`` can never mask a missing key -- what is under test
    is the code, not this machine's environment.  Only throw-away values are
    used; no real secret is written or printed.
    """

    def _import_settings(
        self, extra_env: dict[str, str]
    ) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "config"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            shutil.copy(
                Path(__file__).with_name("settings.py"), package / "settings.py"
            )
            process_env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"SECRET_KEY", "DJANGO_SETTINGS_MODULE"}
            }
            process_env.update(extra_env)
            process_env["PYTHONPATH"] = tmp
            return subprocess.run(
                [sys.executable, "-c", "import config.settings"],
                cwd=tmp,
                env=process_env,
                capture_output=True,
                text=True,
                timeout=60,
            )

    def test_missing_secret_key_is_rejected(self) -> None:
        result = self._import_settings({})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("SECRET_KEY", result.stderr)

    def test_empty_secret_key_is_rejected(self) -> None:
        result = self._import_settings({"SECRET_KEY": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("SECRET_KEY", result.stderr)

    def test_secret_key_from_the_environment_is_accepted(self) -> None:
        result = self._import_settings(
            {"SECRET_KEY": "throwaway-key-for-this-one-test"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
