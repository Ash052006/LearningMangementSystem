"""Model tests for quizzes: constraints, ordering, validators, on_delete."""

import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from courses.models import Course, Enrollment, Lesson

from .forms import ChoiceFormSet
from .models import AttemptAnswer, Choice, Question, Quiz, QuizAttempt
from .services import (
    AttemptsExhaustedError,
    QuizNotGradableError,
    _lock_enrollment,
    grade_attempt,
    submit_attempt,
)

User = get_user_model()


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


def make_question(
    quiz: Quiz, text: str = "What is law?", position: int = 1
) -> Question:
    """Create a question inside ``quiz`` at an explicit position."""
    return Question.objects.create(quiz=quiz, text=text, position=position)


def choice_data(choices: list[tuple[str, bool]], prefix: str = "choices") -> dict:
    """Build the management form and fields for one choice formset."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(choices)),
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for index, (text, correct) in enumerate(choices):
        data[f"{prefix}-{index}-text"] = text
        if correct:
            data[f"{prefix}-{index}-is_correct"] = "on"
    return data


def editor_post(questions: list[list[tuple[str, bool]]], **settings: object) -> dict:
    """Build a POST body that creates ``questions`` from scratch.

    ``questions`` is a list of choice lists, each choice being
    ``(text, is_correct)`` — the same shape the nested editor submits.
    """
    data = {
        "pass_mark": str(settings.get("pass_mark", 70)),
        "max_attempts": str(settings.get("max_attempts", "")),
        "show_answers": str(settings.get("show_answers", "on")),
        "questions-TOTAL_FORMS": str(len(questions)),
        "questions-INITIAL_FORMS": "0",
        "questions-MIN_NUM_FORMS": "0",
        "questions-MAX_NUM_FORMS": "1000",
    }
    for index, choices in enumerate(questions):
        data[f"questions-{index}-text"] = f"Question {index + 1}"
        data.update(choice_data(choices, prefix=f"choices-questions-{index}"))
    return data


def serialize_editor_post(quiz: Quiz, extra_questions: int = 0) -> dict:
    """Serialize an existing quiz exactly as the editor template would.

    Used to edit an existing quiz (keeping the current questions and choices)
    and, optionally, add ``extra_questions`` new questions.
    """
    questions = list(quiz.questions.order_by("position", "pk"))
    data = {
        "pass_mark": str(quiz.pass_mark),
        "max_attempts": "" if quiz.max_attempts is None else str(quiz.max_attempts),
        "show_answers": "on" if quiz.show_answers else "",
        "questions-TOTAL_FORMS": str(len(questions) + extra_questions),
        "questions-INITIAL_FORMS": str(len(questions)),
        "questions-MIN_NUM_FORMS": "0",
        "questions-MAX_NUM_FORMS": "1000",
    }
    for index, question in enumerate(questions):
        data[f"questions-{index}-id"] = str(question.pk)
        data[f"questions-{index}-text"] = question.text
        choices = list(question.choices.all())
        prefix = f"choices-questions-{index}"
        data[f"{prefix}-TOTAL_FORMS"] = str(len(choices))
        data[f"{prefix}-INITIAL_FORMS"] = str(len(choices))
        data[f"{prefix}-MIN_NUM_FORMS"] = "0"
        data[f"{prefix}-MAX_NUM_FORMS"] = "1000"
        for choice_index, choice in enumerate(choices):
            data[f"{prefix}-{choice_index}-id"] = str(choice.pk)
            data[f"{prefix}-{choice_index}-text"] = choice.text
            if choice.is_correct:
                data[f"{prefix}-{choice_index}-is_correct"] = "on"
    for offset in range(extra_questions):
        index = len(questions) + offset
        data[f"questions-{index}-text"] = f"New question {offset + 1}"
        data.update(
            choice_data(
                [("New A", True), ("New B", False)],
                prefix=f"choices-questions-{index}",
            )
        )
    return data


class QuizFixtureMixin(TestCase):
    """Shared quiz fixture: course, enrolment, quiz, question, choices."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="learner")
        self.course = Course.objects.create(
            instructor=make_instructor(), title="Law 101"
        )
        self.enrollment = Enrollment.objects.create(user=self.user, course=self.course)
        self.quiz = Quiz.objects.create(course=self.course)
        # Second course for tests that must not collide with the quiz's
        # OneToOne unique constraint during full_clean().
        self.other_course = Course.objects.create(
            instructor=make_instructor("i2"), title="Other Course"
        )
        self.question = Question.objects.create(
            quiz=self.quiz, text="What is law?", position=1
        )
        self.correct = Choice.objects.create(
            question=self.question, text="Rules", is_correct=True
        )
        self.wrong = Choice.objects.create(question=self.question, text="Chaos")


class QuizModelTests(QuizFixtureMixin):
    """Quiz fields, OneToOne rule and pass_mark validators."""

    def test_str_mentions_quiz_and_course(self) -> None:
        self.assertIn("Law 101", str(self.quiz))

    def test_defaults(self) -> None:
        self.assertEqual(self.quiz.pass_mark, 70)
        self.assertIsNone(self.quiz.max_attempts)
        self.assertTrue(self.quiz.show_answers)

    def test_one_quiz_per_course(self) -> None:
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Quiz.objects.create(course=self.course)

    def test_pass_mark_must_be_at_most_100(self) -> None:
        quiz = Quiz(course=self.other_course, pass_mark=101)
        with self.assertRaisesMessage(ValidationError, "100"):
            quiz.full_clean()

    def test_pass_mark_must_be_at_least_0(self) -> None:
        quiz = Quiz(course=self.other_course, pass_mark=-1)
        with self.assertRaises(ValidationError):
            quiz.full_clean()

    def test_max_attempts_may_be_omitted(self) -> None:
        quiz = Quiz(course=self.other_course, max_attempts=None)
        quiz.full_clean()  # Should not raise.


class QuestionModelTests(QuizFixtureMixin):
    """Question ordering by position."""

    def test_str_shows_position_and_truncated_text(self) -> None:
        self.assertEqual(str(self.question), "Q1: What is law?")

    def test_ordering_by_position(self) -> None:
        second = Question.objects.create(quiz=self.quiz, text="Second?", position=3)
        first = Question.objects.create(quiz=self.quiz, text="First?", position=2)
        # The fixture question already sits at position 1.
        self.assertEqual(
            list(self.quiz.questions.all()), [self.question, first, second]
        )


class ChoiceModelTests(QuizFixtureMixin):
    """At most one correct choice per question (conditional constraint)."""

    def test_str_returns_text(self) -> None:
        self.assertEqual(str(self.correct), "Rules")

    def test_second_correct_choice_for_same_question_rejected(self) -> None:
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Choice.objects.create(
                    question=self.question, text="Also rules", is_correct=True
                )

    def test_unlimited_wrong_choices_allowed(self) -> None:
        Choice.objects.create(question=self.question, text="More chaos")
        Choice.objects.create(question=self.question, text="Even more")
        self.assertEqual(self.question.choices.count(), 4)

    def test_correct_choice_allowed_on_another_question(self) -> None:
        other = Question.objects.create(quiz=self.quiz, text="Why?", position=2)
        Choice.objects.create(question=other, text="Because", is_correct=True)
        self.assertTrue(self.question.choices.filter(is_correct=True).exists())


class QuizAttemptModelTests(QuizFixtureMixin):
    """Attempt ordering, percent validators and the unique answer rule."""

    def _attempt(self, **kwargs: object) -> QuizAttempt:
        defaults: dict[str, object] = {
            "enrollment": self.enrollment,
            "quiz": self.quiz,
            "score": 1,
            "total": 1,
            "percent": Decimal("100.00"),
            "passed": True,
        }
        defaults.update(kwargs)
        return QuizAttempt.objects.create(**defaults)

    def test_str_shows_user_and_percent(self) -> None:
        attempt = self._attempt()
        self.assertIn("learner", str(attempt))
        self.assertIn("100.00", str(attempt))

    def test_ordering_newest_first(self) -> None:
        older = self._attempt()
        newer = self._attempt()
        QuizAttempt.objects.filter(pk=older.pk).update(
            submitted_at=timezone.now() - timedelta(days=1)
        )
        self.assertEqual(list(QuizAttempt.objects.all()), [newer, older])

    def test_percent_must_be_at_most_100(self) -> None:
        attempt = QuizAttempt(
            enrollment=self.enrollment,
            quiz=self.quiz,
            score=2,
            total=1,
            percent=Decimal("101.00"),
        )
        with self.assertRaisesMessage(ValidationError, "100"):
            attempt.full_clean()

    def test_percent_must_be_at_least_0(self) -> None:
        attempt = QuizAttempt(
            enrollment=self.enrollment,
            quiz=self.quiz,
            score=0,
            total=1,
            percent=Decimal("-0.01"),
        )
        with self.assertRaises(ValidationError):
            attempt.full_clean()


class AttemptAnswerModelTests(QuizFixtureMixin):
    """Answer uniqueness plus PROTECT/SET_NULL behaviour."""

    def setUp(self) -> None:
        super().setUp()
        self.attempt = QuizAttempt.objects.create(
            enrollment=self.enrollment,
            quiz=self.quiz,
            score=1,
            total=1,
            percent=Decimal("100.00"),
            passed=True,
        )

    def _answer(self, **kwargs: object) -> AttemptAnswer:
        defaults: dict[str, object] = {
            "attempt": self.attempt,
            "question": self.question,
            "selected_choice": self.correct,
        }
        defaults.update(kwargs)
        return AttemptAnswer.objects.create(**defaults)

    def test_str_shows_question_and_selection(self) -> None:
        answer = self._answer()
        self.assertIn("What is law?", str(answer))
        self.assertIn("Rules", str(answer))

    def test_duplicate_attempt_question_rejected(self) -> None:
        self._answer()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._answer()

    def test_selected_choice_may_be_null(self) -> None:
        answer = self._answer(selected_choice=None)
        self.assertIsNone(answer.selected_choice)
        self.assertIn("no answer", str(answer))

    def test_deleting_answered_question_is_protected(self) -> None:
        self._answer()
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.question.delete()

    def test_deleting_unanswered_question_cascades_choices(self) -> None:
        spare = Question.objects.create(quiz=self.quiz, text="Spare?", position=9)
        Choice.objects.create(question=spare, text="Yes", is_correct=True)
        spare.delete()
        self.assertFalse(Choice.objects.filter(question_id=spare.pk).exists())

    def test_deleting_selected_choice_sets_null(self) -> None:
        answer = self._answer()
        self.correct.delete()
        answer.refresh_from_db()
        self.assertIsNone(answer.selected_choice)
        # The attempt record itself is untouched.
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.percent, Decimal("100.00"))


class CascadeTests(QuizFixtureMixin):
    """Deleting a course or user removes dependent quiz data."""

    def test_deleting_course_removes_quiz_and_questions(self) -> None:
        self.course.delete()
        self.assertFalse(Quiz.objects.exists())
        self.assertFalse(Question.objects.exists())

    def test_deleting_user_removes_enrollment_and_attempts(self) -> None:
        QuizAttempt.objects.create(
            enrollment=self.enrollment,
            quiz=self.quiz,
            score=1,
            total=1,
            percent=Decimal("100.00"),
            passed=True,
        )
        self.user.delete()
        self.assertFalse(Enrollment.objects.exists())
        self.assertFalse(QuizAttempt.objects.exists())

    def test_deleting_course_with_lessons_and_quiz_together(self) -> None:
        Lesson.objects.create(course=self.course, title="Intro", content="x")
        self.course.delete()
        self.assertFalse(Lesson.objects.exists())

    def test_deleting_course_with_answered_quiz_is_protected(self) -> None:
        # Documented policy: AttemptAnswer.question is PROTECT, so a course
        # with historical answers cannot be deleted (raises ProtectedError and
        # deletes nothing).  The P5 delete flow must handle this, see NOTES.md.
        attempt = QuizAttempt.objects.create(
            enrollment=self.enrollment,
            quiz=self.quiz,
            score=1,
            total=1,
            percent=Decimal("100.00"),
            passed=True,
        )
        AttemptAnswer.objects.create(
            attempt=attempt, question=self.question, selected_choice=self.correct
        )
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.course.delete()
        # Nothing is deleted when the cascade is blocked.
        self.assertTrue(Course.objects.filter(pk=self.course.pk).exists())
        self.assertTrue(QuizAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertTrue(AttemptAnswer.objects.filter(attempt=attempt).exists())

    def test_deleting_course_with_unanswered_attempt_cascades(self) -> None:
        # It is the *answers* that protect the course, not the attempts: an
        # attempt with no answer rows deletes with the course.
        attempt = QuizAttempt.objects.create(
            enrollment=self.enrollment,
            quiz=self.quiz,
            score=0,
            total=1,
            percent=Decimal("0.00"),
            passed=False,
        )
        self.course.delete()
        self.assertFalse(QuizAttempt.objects.filter(pk=attempt.pk).exists())


class QuizHasQuestionsTests(TestCase):
    """`Quiz.has_questions` is the "quiz is ready for learners" rule."""

    def test_false_without_questions(self) -> None:
        quiz = Quiz.objects.create(course=make_course(make_instructor()))
        self.assertFalse(quiz.has_questions)

    def test_true_with_a_question(self) -> None:
        quiz = Quiz.objects.create(course=make_course(make_instructor()))
        make_question(quiz)
        self.assertTrue(quiz.has_questions)


class ChoiceFormSetValidationTests(TestCase):
    """The per-question rules: 2 to 6 choices, exactly one correct."""

    def setUp(self) -> None:
        self.quiz = Quiz.objects.create(course=make_course(make_instructor()))
        self.question = make_question(self.quiz)

    def _formset(self, choices: list[tuple[str, bool]]) -> ChoiceFormSet:
        return ChoiceFormSet(
            choice_data(choices), instance=self.question, prefix="choices"
        )

    def test_two_choices_with_one_correct_is_valid(self) -> None:
        formset = self._formset([("Rules", True), ("Chaos", False)])
        self.assertTrue(formset.is_valid())

    def test_zero_correct_is_rejected(self) -> None:
        formset = self._formset([("Rules", False), ("Chaos", False)])
        self.assertFalse(formset.is_valid())
        self.assertIn("exactly one", " ".join(formset.non_form_errors()))

    def test_two_correct_are_rejected(self) -> None:
        formset = self._formset([("Rules", True), ("Chaos", True)])
        self.assertFalse(formset.is_valid())
        self.assertIn("exactly one", " ".join(formset.non_form_errors()))

    def test_single_choice_is_rejected(self) -> None:
        formset = self._formset([("Only one", True)])
        self.assertFalse(formset.is_valid())
        self.assertIn("between 2 and 6", " ".join(formset.non_form_errors()))

    def test_more_than_six_choices_are_rejected(self) -> None:
        formset = self._formset([(f"Choice {i}", i == 0) for i in range(7)])
        self.assertFalse(formset.is_valid())
        self.assertIn("between 2 and 6", " ".join(formset.non_form_errors()))

    def test_exactly_six_choices_is_valid(self) -> None:
        formset = self._formset([(f"Choice {i}", i == 0) for i in range(6)])
        self.assertTrue(formset.is_valid())


class QuizEditorTestCase(TestCase):
    """Shared fixtures for the quiz authoring view tests."""

    def setUp(self) -> None:
        self.instructor = make_instructor("owner")
        self.other_instructor = make_instructor("rival")
        self.learner = make_learner()
        self.course = make_course(self.instructor)
        self.client.force_login(self.instructor)

    def editor_url(self) -> str:
        return reverse("quizzes:quiz_edit", args=[self.course.pk])

    def move_url(self, question: Question, direction: str) -> str:
        return reverse(
            f"quizzes:question_move_{direction}", args=[self.course.pk, question.pk]
        )

    def delete_url(self, question: Question) -> str:
        return reverse("quizzes:question_delete", args=[self.course.pk, question.pk])

    def answer(self, question: Question) -> QuizAttempt:
        """Record an attempt that answered ``question`` (blocks its delete)."""
        enrollment = Enrollment.objects.create(user=self.learner, course=self.course)
        attempt = QuizAttempt.objects.create(
            enrollment=enrollment,
            quiz=question.quiz,
            score=1,
            total=1,
            percent=Decimal("100.00"),
            passed=True,
        )
        AttemptAnswer.objects.create(attempt=attempt, question=question)
        return attempt


class QuizEditorAccessTests(QuizEditorTestCase):
    """Role (403) and ownership (404) checks for the quiz authoring views."""

    def setUp(self) -> None:
        super().setUp()
        self.quiz = Quiz.objects.create(course=self.course)
        self.question = make_question(self.quiz)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.client.logout()
        response = self.client.get(self.editor_url())
        self.assertRedirects(
            response, f"{reverse('accounts:login')}?next={self.editor_url()}"
        )

    def test_learner_gets_403(self) -> None:
        self.client.force_login(self.learner)
        self.assertEqual(self.client.get(self.editor_url()).status_code, 403)

    def test_other_instructor_gets_404_everywhere(self) -> None:
        self.client.force_login(self.other_instructor)
        self.assertEqual(self.client.get(self.editor_url()).status_code, 404)
        # The move/delete endpoints are POST-only, so only POST reaches the
        # ownership check (GET is a 405, covered separately).
        for url in (
            self.move_url(self.question, "up"),
            self.move_url(self.question, "down"),
            self.delete_url(self.question),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url).status_code, 404)


class QuizEditorSaveTests(QuizEditorTestCase):
    """Creating and editing the quiz, questions and choices."""

    def test_get_renders_the_editor_for_the_owner(self) -> None:
        response = self.client.get(self.editor_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Questions")

    def test_post_creates_quiz_questions_and_choices(self) -> None:
        response = self.client.post(
            self.editor_url(),
            editor_post(
                [
                    [("Rules", True), ("Chaos", False)],
                    [("Yes", True), ("No", False)],
                ],
                pass_mark=80,
                max_attempts=3,
                show_answers="",
            ),
        )
        self.assertRedirects(response, self.editor_url())
        quiz = Quiz.objects.get(course=self.course)
        self.assertEqual(quiz.pass_mark, 80)
        self.assertEqual(quiz.max_attempts, 3)
        self.assertFalse(quiz.show_answers)
        self.assertEqual(quiz.questions.count(), 2)
        first = quiz.questions.order_by("position").first()
        self.assertEqual(first.position, 1)
        self.assertEqual(first.choices.count(), 2)
        self.assertEqual(first.choices.filter(is_correct=True).count(), 1)

    def test_post_without_a_correct_choice_saves_nothing(self) -> None:
        response = self.client.post(
            self.editor_url(),
            editor_post([[("Rules", False), ("Chaos", False)]]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("exactly one", response.content.decode())
        self.assertFalse(Quiz.objects.filter(course=self.course).exists())

    def test_post_with_a_single_choice_saves_nothing(self) -> None:
        response = self.client.post(
            self.editor_url(),
            editor_post([[("Only one", True)]]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("between 2 and 6", response.content.decode())
        self.assertFalse(Quiz.objects.filter(course=self.course).exists())

    def test_post_with_blank_question_text_saves_nothing(self) -> None:
        payload = editor_post([[("Rules", True), ("Chaos", False)]])
        payload["questions-0-text"] = ""
        response = self.client.post(self.editor_url(), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Quiz.objects.filter(course=self.course).exists())

    def test_editing_adds_another_question(self) -> None:
        self.client.post(
            self.editor_url(),
            editor_post([[("Rules", True), ("Chaos", False)]]),
        )
        quiz = Quiz.objects.get(course=self.course)
        response = self.client.post(
            self.editor_url(), serialize_editor_post(quiz, extra_questions=1)
        )
        self.assertRedirects(response, self.editor_url())
        self.assertEqual(quiz.questions.count(), 2)
        self.assertEqual(
            list(quiz.questions.values_list("position", flat=True)), [1, 2]
        )

    def test_blank_new_question_row_is_ignored(self) -> None:
        # An untouched extra row (no text) must not create an empty question.
        response = self.client.post(
            self.editor_url(),
            editor_post([[("Rules", True), ("Chaos", False)]]),
        )
        self.assertRedirects(response, self.editor_url())
        quiz = Quiz.objects.get(course=self.course)
        self.assertEqual(quiz.questions.count(), 1)

    def test_get_renders_existing_questions_and_choices(self) -> None:
        self.client.post(
            self.editor_url(),
            editor_post([[("Rules", True), ("Chaos", False)]]),
        )
        response = self.client.get(self.editor_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rules")
        self.assertContains(response, "Chaos")


class QuestionOrderingTests(QuizEditorTestCase):
    """Move up/down swaps neighbouring question positions (POST only)."""

    def setUp(self) -> None:
        super().setUp()
        self.quiz = Quiz.objects.create(course=self.course)
        self.first = make_question(self.quiz, "First", 1)
        self.second = make_question(self.quiz, "Second", 2)

    def test_move_down_swaps_with_next(self) -> None:
        response = self.client.post(self.move_url(self.first, "down"))
        self.assertRedirects(response, self.editor_url())
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.first.position, 2)
        self.assertEqual(self.second.position, 1)

    def test_move_up_swaps_with_previous(self) -> None:
        self.client.post(self.move_url(self.second, "up"))
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.second.position, 1)
        self.assertEqual(self.first.position, 2)

    def test_move_up_at_top_is_a_no_op(self) -> None:
        response = self.client.post(self.move_url(self.first, "up"), follow=True)
        self.assertContains(response, "already at the top")
        self.first.refresh_from_db()
        self.assertEqual(self.first.position, 1)

    def test_move_rejects_get(self) -> None:
        self.assertEqual(
            self.client.get(self.move_url(self.first, "up")).status_code, 405
        )


class QuestionDeleteTests(QuizEditorTestCase):
    """Deleting a question is POST-only and blocked once it has answers."""

    def setUp(self) -> None:
        super().setUp()
        self.quiz = Quiz.objects.create(course=self.course)
        self.question = make_question(self.quiz)

    def test_delete_removes_the_question(self) -> None:
        response = self.client.post(self.delete_url(self.question))
        self.assertRedirects(response, self.editor_url())
        self.assertFalse(Question.objects.filter(pk=self.question.pk).exists())

    def test_delete_rejects_get(self) -> None:
        self.assertEqual(
            self.client.get(self.delete_url(self.question)).status_code, 405
        )

    def test_delete_is_blocked_when_the_question_has_attempts(self) -> None:
        self.answer(self.question)
        response = self.client.post(self.delete_url(self.question), follow=True)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Question.objects.filter(pk=self.question.pk).exists())
        # The attempt itself is never touched.
        self.assertTrue(AttemptAnswer.objects.filter(question=self.question).exists())


class QuizHistoryWarningTests(QuizEditorTestCase):
    """The editor warns, without deleting anything, when attempts exist."""

    def test_warning_is_shown_when_attempts_exist(self) -> None:
        quiz = Quiz.objects.create(course=self.course)
        question = make_question(quiz)
        self.answer(question)
        response = self.client.get(self.editor_url())
        self.assertContains(response, "already has recorded attempts")

    def test_no_warning_without_attempts(self) -> None:
        Quiz.objects.create(course=self.course)
        response = self.client.get(self.editor_url())
        self.assertNotContains(response, "already has recorded attempts")


class QuizPublishVisibilityTests(QuizEditorTestCase):
    """An empty quiz is published-but-hidden; the publish step warns."""

    def setUp(self) -> None:
        super().setUp()
        Lesson.objects.create(course=self.course, title="Intro", content="x")
        self.publish_url = reverse(
            "courses:course_toggle_publish", args=[self.course.pk]
        )

    def test_empty_quiz_warns_on_publish(self) -> None:
        Quiz.objects.create(course=self.course)
        response = self.client.post(self.publish_url, follow=True)
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PUBLISHED)
        self.assertContains(response, "will not be shown to learners")

    def test_quiz_with_a_question_does_not_warn(self) -> None:
        quiz = Quiz.objects.create(course=self.course)
        make_question(quiz)
        response = self.client.post(self.publish_url, follow=True)
        self.assertNotContains(response, "will not be shown to learners")

    def test_course_without_a_quiz_publishes_without_the_quiz_warning(self) -> None:
        # The quiz warning must not affect the P5 rule for ordinary courses.
        response = self.client.post(self.publish_url, follow=True)
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PUBLISHED)
        self.assertNotContains(response, "will not be shown to learners")

    def test_instructor_course_page_flags_an_empty_quiz(self) -> None:
        Quiz.objects.create(course=self.course)
        response = self.client.get(
            reverse("courses:course_detail", args=[self.course.pk])
        )
        self.assertContains(response, "hidden from learners")

    def test_instructor_course_page_reports_a_ready_quiz(self) -> None:
        quiz = Quiz.objects.create(course=self.course)
        make_question(quiz)
        response = self.client.get(
            reverse("courses:course_detail", args=[self.course.pk])
        )
        self.assertNotContains(response, "hidden from learners")
        self.assertContains(response, "1 question")


class QuizLearnerExposureTests(TestCase):
    """P7 adds no learner quiz route; quiz content must not reach learners.

    The learner-facing quiz flow is a later task. Until it exists the only
    quiz routes are instructor management routes, and a quiz with no questions
    must not be reachable from anywhere a learner can go.
    """

    def setUp(self) -> None:
        self.instructor = make_instructor("owner")
        self.learner = make_learner()
        self.course = make_course(self.instructor, "Course")
        self.course.status = Course.Status.PUBLISHED
        self.course.save()
        Lesson.objects.create(course=self.course, title="Intro", content="x")
        self.quiz = Quiz.objects.create(course=self.course)
        self.client.force_login(self.learner)

    def learner_course_url(self) -> str:
        return reverse("courses:learner_course_detail", args=[self.course.pk])

    def test_empty_quiz_is_not_exposed_to_a_learner(self) -> None:
        response = self.client.get(self.learner_course_url())
        self.assertEqual(response.status_code, 200)
        # No link to the editor and no visibility notice on the learner page.
        self.assertNotContains(
            response, reverse("quizzes:quiz_edit", args=[self.course.pk])
        )
        self.assertNotContains(response, "hidden from learners")
        # The only quiz route refuses a learner outright.
        self.assertEqual(
            self.client.get(
                reverse("quizzes:quiz_edit", args=[self.course.pk])
            ).status_code,
            403,
        )

    def test_guessed_learner_quiz_urls_are_not_routed(self) -> None:
        for url in (
            f"/quizzes/{self.course.pk}/",
            f"/quizzes/{self.course.pk}/questions/",
            f"/courses/{self.course.pk}/quiz/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_adding_a_valid_question_marks_the_quiz_ready(self) -> None:
        self.assertFalse(self.quiz.has_questions)
        make_question(self.quiz, "A real question")
        self.quiz.refresh_from_db()
        self.assertTrue(self.quiz.has_questions)
        # Adding a question does not change the course's publication state.
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PUBLISHED)

    def test_adding_a_question_does_not_leak_it_to_the_course_page(self) -> None:
        make_question(self.quiz, "Secret question")
        response = self.client.get(self.learner_course_url())
        self.assertNotContains(response, "Secret question")


class QuizTakingTestCase(TestCase):
    """Shared fixture for the P8 grading, taking, result and preview tests."""

    def setUp(self) -> None:
        self.instructor = make_instructor("owner")
        self.learner = make_learner("learner")
        self.course = make_course(self.instructor, "Course")
        self.course.status = Course.Status.PUBLISHED
        self.course.save()
        Lesson.objects.create(course=self.course, title="Intro", content="x")
        self.quiz = Quiz.objects.create(course=self.course)
        self.enrollment = Enrollment.objects.create(
            user=self.learner, course=self.course
        )
        self.client.force_login(self.learner)

    def add_question(
        self, text: str, correct: int = 0, count: int = 2
    ) -> tuple[Question, list[Choice]]:
        """Add a question with ``count`` choices, ``correct`` being the right one."""
        question = make_question(self.quiz, text, self.quiz.questions.count() + 1)
        choices = [
            Choice.objects.create(
                question=question,
                text=f"{text} option {index}",
                is_correct=(index == correct),
            )
            for index in range(count)
        ]
        return question, choices

    def add_questions(self, count: int) -> list[tuple[Question, list[Choice]]]:
        """Add ``count`` two-choice questions (first choice correct)."""
        return [self.add_question(f"Q{index}", correct=0) for index in range(count)]

    def payload(self, answers: dict[Question, Choice | None]) -> dict[str, str]:
        """Build a submission body: question -> chosen choice (or no answer)."""
        data: dict[str, str] = {}
        for question, choice in answers.items():
            if choice is not None:
                data[f"question_{question.pk}"] = str(choice.pk)
        return data

    def take_url(self) -> str:
        return reverse("quizzes:take", args=[self.course.pk])

    def result_url(self, attempt: QuizAttempt) -> str:
        return reverse("quizzes:result", args=[self.course.pk, attempt.pk])

    def preview_url(self) -> str:
        return reverse("quizzes:preview", args=[self.course.pk])


class GradeAttemptTests(QuizTakingTestCase):
    """``grade_attempt`` is pure: correctness comes from the database."""

    def setUp(self) -> None:
        super().setUp()
        self.questions = self.add_questions(3)
        self.q1, self.q1_choices = self.questions[0]
        self.q2, self.q2_choices = self.questions[1]
        self.q3, self.q3_choices = self.questions[2]

    def test_all_correct(self) -> None:
        result = grade_attempt(
            self.quiz,
            {q.pk: choices[0].pk for q, choices in self.questions},
        )
        self.assertEqual(result.score, 3)
        self.assertEqual(result.total, 3)
        self.assertEqual(result.percent, Decimal("100.00"))
        self.assertTrue(result.passed)
        self.assertTrue(all(result.per_question.values()))

    def test_all_wrong(self) -> None:
        result = grade_attempt(
            self.quiz,
            {q.pk: choices[1].pk for q, choices in self.questions},
        )
        self.assertEqual(result.score, 0)
        self.assertEqual(result.percent, Decimal("0.00"))
        self.assertFalse(result.passed)
        self.assertFalse(any(result.per_question.values()))

    def test_partial_score(self) -> None:
        result = grade_attempt(
            self.quiz,
            {
                self.q1.pk: self.q1_choices[0].pk,
                self.q2.pk: self.q2_choices[0].pk,
                self.q3.pk: self.q3_choices[1].pk,
            },
        )
        self.assertEqual(result.score, 2)
        self.assertEqual(result.percent, Decimal("66.67"))
        self.assertFalse(result.passed)  # Below the default 70% pass mark.
        self.assertEqual(
            result.per_question,
            {self.q1.pk: True, self.q2.pk: True, self.q3.pk: False},
        )

    def test_unanswered_counts_as_wrong(self) -> None:
        result = grade_attempt(
            self.quiz,
            {self.q1.pk: self.q1_choices[0].pk, self.q2.pk: None},
        )
        self.assertEqual(result.score, 1)
        self.assertFalse(result.per_question[self.q2.pk])
        self.assertFalse(result.per_question[self.q3.pk])

    def test_foreign_choice_id_is_ignored(self) -> None:
        # A correct choice that belongs to another question never earns a mark.
        result = grade_attempt(
            self.quiz,
            {self.q1.pk: self.q2_choices[0].pk},
        )
        self.assertFalse(result.per_question[self.q1.pk])
        self.assertEqual(result.score, 0)

    def test_tampered_question_id_is_ignored(self) -> None:
        result = grade_attempt(
            self.quiz,
            {self.q1.pk: self.q1_choices[0].pk, 999999: self.q2_choices[0].pk},
        )
        self.assertNotIn(999999, result.per_question)
        self.assertEqual(result.total, 3)
        self.assertEqual(result.score, 1)

    def test_per_question_covers_every_question(self) -> None:
        result = grade_attempt(self.quiz, {})
        self.assertEqual(set(result.per_question), {q.pk for q, _ in self.questions})

    def test_zero_question_quiz_raises(self) -> None:
        empty = Quiz.objects.create(course=make_course(make_instructor("i2"), "Empty"))
        with self.assertRaises(QuizNotGradableError):
            grade_attempt(empty, {})


class GradeBoundaryTests(QuizTakingTestCase):
    """The pass decision is exactly ``percent >= pass_mark``."""

    def test_exactly_at_pass_mark_passes(self) -> None:
        questions = self.add_questions(10)
        answers = {q.pk: choices[0].pk for q, choices in questions[:7]}
        result = grade_attempt(self.quiz, answers)
        self.assertEqual(result.percent, Decimal("70.00"))
        self.assertTrue(result.passed)

    def test_below_pass_mark_fails(self) -> None:
        questions = self.add_questions(10)
        answers = {q.pk: choices[0].pk for q, choices in questions[:6]}
        result = grade_attempt(self.quiz, answers)
        self.assertEqual(result.percent, Decimal("60.00"))
        self.assertFalse(result.passed)

    def test_sixty_nine_percent_fails(self) -> None:
        questions = self.add_questions(13)
        answers = {q.pk: choices[0].pk for q, choices in questions[:9]}
        result = grade_attempt(self.quiz, answers)
        self.assertEqual(result.percent, Decimal("69.23"))
        self.assertFalse(result.passed)

    def test_pass_mark_is_configurable(self) -> None:
        questions = self.add_questions(10)
        self.quiz.pass_mark = 60
        self.quiz.save()
        answers = {q.pk: choices[0].pk for q, choices in questions[:6]}
        result = grade_attempt(self.quiz, answers)
        self.assertEqual(result.percent, Decimal("60.00"))
        self.assertTrue(result.passed)


class SubmitAttemptTests(QuizTakingTestCase):
    """``submit_attempt`` writes immutable rows and enforces max_attempts."""

    def setUp(self) -> None:
        super().setUp()
        self.questions = self.add_questions(3)

    def test_creates_an_attempt_with_one_answer_per_question(self) -> None:
        questions = self.questions
        attempt = submit_attempt(
            self.enrollment,
            self.quiz,
            self.payload({q: choices[0] for q, choices in questions}),
        )
        self.assertEqual(attempt.score, 3)
        self.assertEqual(attempt.total, 3)
        self.assertEqual(attempt.percent, Decimal("100.00"))
        self.assertTrue(attempt.passed)
        self.assertEqual(attempt.answers.count(), 3)

    def test_unanswered_is_stored_as_no_choice(self) -> None:
        question, choices = self.questions[0]
        attempt = submit_attempt(
            self.enrollment, self.quiz, self.payload({question: None})
        )
        answer = attempt.answers.get(question=question)
        self.assertIsNone(answer.selected_choice)
        self.assertEqual(attempt.score, 0)

    def test_foreign_choice_id_is_stored_as_no_choice(self) -> None:
        first, first_choices = self.questions[0]
        _second, second_choices = self.questions[1]
        attempt = submit_attempt(
            self.enrollment, self.quiz, self.payload({first: second_choices[0]})
        )
        answer = attempt.answers.get(question=first)
        self.assertIsNone(answer.selected_choice)
        self.assertEqual(attempt.score, 0)

    def test_max_attempts_is_enforced(self) -> None:
        self.quiz.max_attempts = 1
        self.quiz.save()
        payload = self.payload({self.questions[0][0]: self.questions[0][1][0]})
        submit_attempt(self.enrollment, self.quiz, payload)
        with self.assertRaises(AttemptsExhaustedError):
            submit_attempt(self.enrollment, self.quiz, payload)
        self.assertEqual(
            QuizAttempt.objects.filter(
                enrollment=self.enrollment, quiz=self.quiz
            ).count(),
            1,
        )

    def test_attempts_accumulate_and_are_never_overwritten(self) -> None:
        question, choices = self.questions[0]
        first = submit_attempt(
            self.enrollment, self.quiz, self.payload({question: choices[0]})
        )
        second = submit_attempt(
            self.enrollment, self.quiz, self.payload({question: choices[1]})
        )
        first.refresh_from_db()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.score, 1)  # The first attempt is untouched.
        self.assertEqual(second.score, 0)
        self.assertEqual(
            QuizAttempt.objects.filter(
                enrollment=self.enrollment, quiz=self.quiz
            ).count(),
            2,
        )

    def test_unlimited_attempts_allowed_when_max_attempts_is_none(self) -> None:
        question, choices = self.questions[0]
        for _ in range(4):
            submit_attempt(
                self.enrollment, self.quiz, self.payload({question: choices[0]})
            )
        self.assertEqual(
            QuizAttempt.objects.filter(
                enrollment=self.enrollment, quiz=self.quiz
            ).count(),
            4,
        )

    def test_zero_question_quiz_saves_nothing(self) -> None:
        empty = Quiz.objects.create(course=make_course(make_instructor("i2"), "Empty"))
        enrollment = Enrollment.objects.create(user=self.learner, course=empty.course)
        with self.assertRaises(QuizNotGradableError):
            submit_attempt(enrollment, empty, {})
        self.assertFalse(QuizAttempt.objects.filter(quiz=empty).exists())

    def test_percentage_is_stored_with_two_decimals(self) -> None:
        attempt = submit_attempt(
            self.enrollment,
            self.quiz,
            self.payload({self.questions[0][0]: self.questions[0][1][0]}),
        )
        self.assertEqual(attempt.percent, Decimal("33.33"))


class TakeQuizViewTests(QuizTakingTestCase):
    """GET renders the unanswered quiz; POST grades and redirects."""

    def setUp(self) -> None:
        super().setUp()
        self.questions = self.add_questions(3)
        self.q1, self.q1_choices = self.questions[0]

    def test_get_renders_every_question_and_choice(self) -> None:
        response = self.client.get(self.take_url())
        self.assertEqual(response.status_code, 200)
        for question, _choices in self.questions:
            self.assertContains(response, question.text)
        self.assertContains(response, 'type="radio"')

    def test_get_never_reveals_correctness(self) -> None:
        response = self.client.get(self.take_url())
        body = response.content.decode()
        self.assertNotIn("is_correct", body)
        self.assertNotIn("Correct", body)

    def test_post_grades_and_redirects_to_the_result(self) -> None:
        response = self.client.post(
            self.take_url(),
            self.payload({q: choices[0] for q, choices in self.questions}),
        )
        attempt = QuizAttempt.objects.get(enrollment=self.enrollment)
        self.assertRedirects(response, self.result_url(attempt))
        self.assertEqual(attempt.score, 3)

    def test_post_with_unanswered_questions_scores_zero_for_them(self) -> None:
        self.client.post(self.take_url(), self.payload({self.q1: self.q1_choices[0]}))
        attempt = QuizAttempt.objects.get(enrollment=self.enrollment)
        self.assertEqual(attempt.score, 1)
        self.assertEqual(attempt.total, 3)

    def test_post_with_a_foreign_choice_is_ignored(self) -> None:
        _other, other_choices = self.questions[1]
        self.client.post(self.take_url(), self.payload({self.q1: other_choices[0]}))
        attempt = QuizAttempt.objects.get(enrollment=self.enrollment)
        self.assertEqual(attempt.score, 0)

    def test_empty_quiz_get_is_404(self) -> None:
        self.quiz.questions.all().delete()
        self.assertEqual(self.client.get(self.take_url()).status_code, 404)

    def test_empty_quiz_post_is_404_and_saves_nothing(self) -> None:
        self.quiz.questions.all().delete()
        self.assertEqual(self.client.post(self.take_url(), {}).status_code, 404)
        self.assertFalse(QuizAttempt.objects.exists())

    def test_draft_course_is_404_on_get_and_post(self) -> None:
        self.course.status = Course.Status.DRAFT
        self.course.save()
        self.assertEqual(self.client.get(self.take_url()).status_code, 404)
        self.assertEqual(self.client.post(self.take_url(), {}).status_code, 404)

    def test_non_enrolled_learner_gets_403_on_get_and_post(self) -> None:
        stranger = make_learner("stranger")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.take_url()).status_code, 403)
        self.assertEqual(self.client.post(self.take_url(), {}).status_code, 403)

    def test_instructors_are_not_learners_here(self) -> None:
        self.client.force_login(self.instructor)
        self.assertEqual(self.client.get(self.take_url()).status_code, 403)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.client.logout()
        response = self.client.get(self.take_url())
        self.assertRedirects(
            response, f"{reverse('accounts:login')}?next={self.take_url()}"
        )
        self.assertEqual(self.client.post(self.take_url(), {}).status_code, 302)

    def test_exhausted_attempts_redirect_get_to_the_latest_result(self) -> None:
        self.quiz.max_attempts = 1
        self.quiz.save()
        attempt = submit_attempt(
            self.enrollment,
            self.quiz,
            self.payload({self.q1: self.q1_choices[0]}),
        )
        self.assertRedirects(self.client.get(self.take_url()), self.result_url(attempt))

    def test_exhausted_attempts_reject_post(self) -> None:
        self.quiz.max_attempts = 1
        self.quiz.save()
        attempt = submit_attempt(
            self.enrollment,
            self.quiz,
            self.payload({self.q1: self.q1_choices[0]}),
        )
        response = self.client.post(self.take_url(), self.payload({self.q1: None}))
        self.assertRedirects(response, self.result_url(attempt))
        self.assertEqual(
            QuizAttempt.objects.filter(enrollment=self.enrollment).count(), 1
        )


class QuizResultViewTests(QuizTakingTestCase):
    """The result page: score, history, review and retake rules."""

    def setUp(self) -> None:
        super().setUp()
        self.questions = self.add_questions(10)
        self.payload_all_correct = self.payload(
            {q: choices[0] for q, choices in self.questions}
        )
        self.attempt = submit_attempt(
            self.enrollment, self.quiz, self.payload_all_correct
        )

    def test_shows_score_percent_and_pass_mark(self) -> None:
        response = self.client.get(self.result_url(self.attempt))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10 of 10")
        self.assertContains(response, "100.00")
        self.assertContains(response, "Pass mark: 70%")

    def test_shows_passed_verdict(self) -> None:
        response = self.client.get(self.result_url(self.attempt))
        self.assertContains(response, "Passed")

    def test_shows_failed_verdict(self) -> None:
        failed = submit_attempt(self.enrollment, self.quiz, {})
        response = self.client.get(self.result_url(failed))
        self.assertContains(response, "Failed")

    def test_shows_the_attempt_history(self) -> None:
        submit_attempt(self.enrollment, self.quiz, {})
        response = self.client.get(self.result_url(self.attempt))
        self.assertContains(response, "Attempt history")
        self.assertContains(response, "This attempt")

    def test_review_is_shown_when_the_quiz_allows_it(self) -> None:
        response = self.client.get(self.result_url(self.attempt))
        self.assertContains(response, "Answer review")
        self.assertContains(response, "Your answer:")

    def test_review_is_hidden_when_show_answers_is_false(self) -> None:
        self.quiz.show_answers = False
        self.quiz.save()
        response = self.client.get(self.result_url(self.attempt))
        self.assertNotContains(response, "Answer review")

    def test_retake_link_is_shown_while_attempts_remain(self) -> None:
        response = self.client.get(self.result_url(self.attempt))
        self.assertContains(response, "Retake quiz")

    def test_retake_link_is_hidden_when_attempts_are_exhausted(self) -> None:
        self.quiz.max_attempts = 1
        self.quiz.save()
        response = self.client.get(self.result_url(self.attempt))
        self.assertNotContains(response, "Retake quiz")

    def test_another_learners_attempt_is_404(self) -> None:
        other = make_learner("other")
        other_enrollment = Enrollment.objects.create(user=other, course=self.course)
        other_attempt = submit_attempt(other_enrollment, self.quiz, {})
        self.assertEqual(
            self.client.get(self.result_url(other_attempt)).status_code, 404
        )

    def test_draft_course_result_is_404(self) -> None:
        self.course.status = Course.Status.DRAFT
        self.course.save()
        self.assertEqual(
            self.client.get(self.result_url(self.attempt)).status_code, 404
        )

    def test_non_enrolled_learner_gets_403(self) -> None:
        self.client.force_login(make_learner("stranger"))
        self.assertEqual(
            self.client.get(self.result_url(self.attempt)).status_code, 403
        )

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.client.logout()
        url = self.result_url(self.attempt)
        self.assertRedirects(
            self.client.get(url), f"{reverse('accounts:login')}?next={url}"
        )


class QuizPreviewTests(QuizTakingTestCase):
    """Instructors preview their own quiz; the preview saves nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.questions = self.add_questions(3)
        self.q1, self.q1_choices = self.questions[0]
        self.client.force_login(self.instructor)

    def test_instructor_can_open_the_preview(self) -> None:
        response = self.client.get(self.preview_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Preview mode")
        self.assertNotContains(response, "is_correct")

    def test_preview_post_grades_without_saving(self) -> None:
        response = self.client.post(
            self.preview_url(),
            self.payload({q: choices[0] for q, choices in self.questions}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "3 of 3")
        self.assertContains(response, "Answer review")
        self.assertFalse(QuizAttempt.objects.exists())
        self.assertFalse(AttemptAnswer.objects.exists())

    def test_preview_shows_review_even_when_learners_cannot_see_answers(self) -> None:
        self.quiz.show_answers = False
        self.quiz.save()
        response = self.client.post(
            self.preview_url(),
            self.payload({self.q1: self.q1_choices[0]}),
        )
        self.assertContains(response, "Answer review")

    def test_learner_gets_403(self) -> None:
        self.client.force_login(self.learner)
        self.assertEqual(self.client.get(self.preview_url()).status_code, 403)

    def test_other_instructor_gets_404(self) -> None:
        self.client.force_login(make_instructor("rival"))
        self.assertEqual(self.client.get(self.preview_url()).status_code, 404)
        self.assertEqual(self.client.post(self.preview_url(), {}).status_code, 404)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.client.logout()
        self.assertRedirects(
            self.client.get(self.preview_url()),
            f"{reverse('accounts:login')}?next={self.preview_url()}",
        )

    def test_empty_quiz_preview_is_404(self) -> None:
        self.quiz.questions.all().delete()
        self.assertEqual(self.client.get(self.preview_url()).status_code, 404)


class LearnerQuizLinkTests(QuizTakingTestCase):
    """The learner course page links to the quiz once it is ready."""

    def course_url(self) -> str:
        return reverse("courses:learner_course_detail", args=[self.course.pk])

    def test_link_is_shown_when_the_quiz_has_questions(self) -> None:
        self.add_question("A real question")
        response = self.client.get(self.course_url())
        self.assertContains(response, self.take_url())

    def test_link_is_hidden_when_the_quiz_is_empty(self) -> None:
        response = self.client.get(self.course_url())
        self.assertNotContains(response, self.take_url())

    def test_link_is_hidden_without_a_quiz(self) -> None:
        self.quiz.delete()
        response = self.client.get(self.course_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("quizzes:take", args=[self.course.pk]))


class SubmissionLockingTests(QuizTakingTestCase):
    """SQLite has no row locks, so submission must lock before it counts."""

    def setUp(self) -> None:
        super().setUp()
        self.question, self.choices = self.add_question("Q")

    def test_sqlite_write_lock_precedes_the_attempt_count(self) -> None:
        self.quiz.max_attempts = 2
        self.quiz.save()
        payload = self.payload({self.question: self.choices[0]})
        with CaptureQueriesContext(connection) as captured:
            submit_attempt(self.enrollment, self.quiz, payload)
        sql = [query["sql"] for query in captured.captured_queries]
        lock_index = next(
            index
            for index, statement in enumerate(sql)
            if statement.lstrip().upper().startswith("UPDATE")
            and "courses_enrollment" in statement
        )
        count_index = next(
            index
            for index, statement in enumerate(sql)
            if "COUNT" in statement.upper() and "quizzes_quizattempt" in statement
        )
        self.assertLess(lock_index, count_index)

    def test_lock_helper_uses_an_update_when_row_locks_are_unavailable(self) -> None:
        # Guard the assumption behind the fallback: SQLite has no row locks.
        self.assertFalse(connection.features.has_select_for_update)
        with CaptureQueriesContext(connection) as captured:
            _lock_enrollment(self.enrollment)
        self.assertEqual(len(captured.captured_queries), 1)
        statement = captured.captured_queries[0]["sql"]
        self.assertIn("UPDATE", statement.upper())
        self.assertIn("courses_enrollment", statement)


class AttemptRollbackTests(QuizTakingTestCase):
    """A failure while writing answers must leave no attempt behind."""

    def setUp(self) -> None:
        super().setUp()
        self.questions = self.add_questions(2)

    def test_answer_persistence_failure_rolls_back_the_attempt(self) -> None:
        payload = self.payload({q: choices[0] for q, choices in self.questions})
        with mock.patch.object(
            AttemptAnswer.objects,
            "bulk_create",
            side_effect=IntegrityError("answer insert failed"),
        ):
            with self.assertRaises(IntegrityError):
                submit_attempt(self.enrollment, self.quiz, payload)
        self.assertFalse(
            QuizAttempt.objects.filter(enrollment=self.enrollment).exists()
        )
        self.assertFalse(AttemptAnswer.objects.exists())

    def test_failed_submission_does_not_consume_an_attempt(self) -> None:
        # A rolled-back attempt must not count against max_attempts, so the
        # learner can still submit successfully afterwards.
        self.quiz.max_attempts = 1
        self.quiz.save()
        payload = self.payload({q: choices[0] for q, choices in self.questions})
        with mock.patch.object(
            AttemptAnswer.objects,
            "bulk_create",
            side_effect=IntegrityError("answer insert failed"),
        ):
            with self.assertRaises(IntegrityError):
                submit_attempt(self.enrollment, self.quiz, payload)
        attempt = submit_attempt(self.enrollment, self.quiz, payload)
        self.assertEqual(attempt.score, 2)


class ConcurrentSubmissionTests(TransactionTestCase):
    """Two concurrent submissions must not exceed ``max_attempts``.

    The test runner's shared-cache in-memory SQLite surfaces the loser of a
    write race as a coarse ``OperationalError`` rather than a clean rejection,
    so the assertion here is the invariant that matters: at most ``max_attempts``
    attempts commit, each with a complete set of answers.  The production-like
    file-backed behaviour is verified separately in NOTES.md.
    """

    def setUp(self) -> None:
        self.instructor = make_instructor("owner")
        self.learner = make_learner("learner")
        self.course = make_course(self.instructor, "Course")
        self.course.status = Course.Status.PUBLISHED
        self.course.save()
        Lesson.objects.create(course=self.course, title="Intro", content="x")
        self.quiz = Quiz.objects.create(course=self.course, max_attempts=1)
        self.question = make_question(self.quiz, "Q", 1)
        self.choice = Choice.objects.create(
            question=self.question, text="A", is_correct=True
        )
        self.enrollment = Enrollment.objects.create(
            user=self.learner, course=self.course
        )

    def test_two_concurrent_submissions_never_exceed_the_limit(self) -> None:
        payload = {f"question_{self.question.pk}": str(self.choice.pk)}
        barrier = threading.Barrier(2)
        unexpected: list[BaseException] = []

        def submit() -> None:
            try:
                barrier.wait(timeout=10)
                submit_attempt(self.enrollment, self.quiz, payload)
            except AttemptsExhaustedError:
                pass
            except OperationalError:
                # SQLite lock contention: this writer committed nothing.
                pass
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                unexpected.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(unexpected, [])
        attempts = QuizAttempt.objects.filter(
            enrollment=self.enrollment, quiz=self.quiz
        )
        self.assertLessEqual(attempts.count(), 1)
        for attempt in attempts:
            self.assertEqual(attempt.answers.count(), 1)
