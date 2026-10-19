"""Business logic for quizzes: authoring, grading and immutable attempts.

Views stay thin: they validate access, call one of these functions, and turn
the outcome into a message.  Attempts are immutable records (see models and
NOTES.md), so nothing here ever updates or deletes an ``AttemptAnswer``.

Grading is a pure function of the quiz and the submitted answers: the correct
choices are read from the database, never trusted from the request, so a
tampered or foreign choice id cannot earn a mark.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import connection, transaction
from django.db.models import F

from courses.models import Enrollment

from .models import AttemptAnswer, Question, Quiz, QuizAttempt


def question_has_attempts(question: Question) -> bool:
    """True when any attempt already answered ``question``.

    ``AttemptAnswer.question`` is ``PROTECT``, so deleting such a question is
    impossible without destroying assessment history.  Callers use this to
    refuse the delete with a clear message instead of hitting a 500.
    """
    return AttemptAnswer.objects.filter(question=question).exists()


def resequence_questions(quiz: Quiz) -> None:
    """Renumber a quiz's questions to positions 1..n, keeping the current order.

    ``Question`` has no (quiz, position) uniqueness (only ``Lesson`` does), so
    a single pass with plain updates is enough; the transaction keeps the
    numbering consistent if anything fails halfway.
    """
    questions = list(quiz.questions.order_by("position", "pk"))
    with transaction.atomic():
        for position, question in enumerate(questions, start=1):
            Question.objects.filter(pk=question.pk).update(position=position)


def move_question(question: Question, offset: int) -> bool:
    """Move ``question`` one place up (offset -1) or down (+1).

    Returns ``False`` when the question is already at the edge and nothing
    changes; otherwise swaps it with its neighbour and renumbers the quiz.
    """
    questions = list(question.quiz.questions.order_by("position", "pk"))
    index = questions.index(question)
    target = index + offset
    if not 0 <= target < len(questions):
        return False
    questions[index], questions[target] = questions[target], questions[index]
    with transaction.atomic():
        for position, moved in enumerate(questions, start=1):
            Question.objects.filter(pk=moved.pk).update(position=position)
    return True


def move_question_up(question: Question) -> bool:
    """Move a question one position towards the start of its quiz."""
    return move_question(question, -1)


def move_question_down(question: Question) -> bool:
    """Move a question one position towards the end of its quiz."""
    return move_question(question, 1)


class QuizNotGradableError(Exception):
    """Raised when a quiz with no questions is graded or submitted."""


class AttemptsExhaustedError(Exception):
    """Raised when a learner has used every attempt the quiz allows."""


@dataclass(frozen=True)
class GradeResult:
    """Outcome of grading one set of answers.

    ``per_question`` maps each question's primary key to whether it was
    answered correctly.  It covers every question in the quiz (unanswered ones
    are ``False``) and never includes ids the quiz does not own.
    """

    score: int
    total: int
    percent: Decimal
    passed: bool
    per_question: dict[int, bool]


def answers_from_post(
    quiz: Quiz, post_data: Mapping[str, Any]
) -> dict[int, int | None]:
    """Read one ``question_<pk>`` radio value per question from submitted data.

    Missing, blank or non-numeric values become ``None`` (no answer).  The
    values are *not* checked against the question here: grading does that.
    """
    answers: dict[int, int | None] = {}
    for question in quiz.questions.all():
        raw = post_data.get(f"question_{question.pk}")
        if raw in (None, ""):
            answers[question.pk] = None
            continue
        try:
            answers[question.pk] = int(raw)
        except (TypeError, ValueError):
            answers[question.pk] = None
    return answers


def _evaluate(
    questions: list[Question], answers: Mapping[int, int | None]
) -> tuple[dict[int, int | None], dict[int, bool], int]:
    """Grade ``answers`` against ``questions``.

    Returns the sanitised selections (each choice id is checked to belong to
    its question, otherwise ``None``), the per-question correctness map and the
    raw score.  ``questions`` must have ``choices`` prefetched.
    """
    sanitised: dict[int, int | None] = {}
    per_question: dict[int, bool] = {}
    score = 0
    for question in questions:
        choices = list(question.choices.all())
        correct_id = next((choice.pk for choice in choices if choice.is_correct), None)
        selected = answers.get(question.pk)
        if selected is None or not any(choice.pk == selected for choice in choices):
            # A choice that belongs to another question, or no answer at all,
            # is never correct.
            selected = None
        sanitised[question.pk] = selected
        correct = selected is not None and selected == correct_id
        per_question[question.pk] = correct
        score += int(correct)
    return sanitised, per_question, score


def _summarise(quiz: Quiz, score: int, total: int) -> tuple[Decimal, bool]:
    """Turn a raw score into a two-decimal percentage and a pass/fail flag."""
    percent = (Decimal(score) / Decimal(total) * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return percent, percent >= Decimal(quiz.pass_mark)


def grade_attempt(quiz: Quiz, answers: Mapping[int, int | None]) -> GradeResult:
    """Grade ``answers`` against ``quiz`` without touching the database.

    ``answers`` maps a question's primary key to the chosen choice's primary
    key (or ``None`` for no answer).  Choice ids that do not belong to their
    question, and question ids that are not part of the quiz, are ignored; an
    unanswered question counts as wrong.  A quiz with no questions cannot be
    graded and raises ``QuizNotGradableError``.
    """
    questions = list(quiz.questions.prefetch_related("choices"))
    if not questions:
        raise QuizNotGradableError("A quiz with no questions cannot be graded.")
    _, per_question, score = _evaluate(questions, answers)
    total = len(questions)
    percent, passed = _summarise(quiz, score, total)
    return GradeResult(score, total, percent, passed, per_question)


def _lock_enrollment(enrollment: Enrollment) -> None:
    """Serialise attempt creation for one enrolment.

    ``select_for_update()`` relies on ``SELECT ... FOR UPDATE``, which SQLite
    does not implement (``connection.features.has_select_for_update`` is False),
    so on SQLite it is a silent no-op: two submissions could both read "0
    attempts" and both insert.  SQLite *does* serialise writers, so on backends
    without row locks we take the database write lock up front with a harmless
    self-update.  The second submission then waits for the first to commit and
    its later count sees the new attempt, turning the race into a clean
    ``AttemptsExhaustedError`` instead of an over-limit insert.  Backends that
    support row locking (e.g. PostgreSQL) lock the enrolment row directly.
    """
    if connection.features.has_select_for_update:
        Enrollment.objects.select_for_update().get(pk=enrollment.pk)
    else:
        Enrollment.objects.filter(pk=enrollment.pk).update(enrolled_at=F("enrolled_at"))


def submit_attempt(
    enrollment: Enrollment, quiz: Quiz, post_data: Mapping[str, Any]
) -> QuizAttempt:
    """Create an immutable ``QuizAttempt`` (and its answers) for a submission.

    The attempt and one ``AttemptAnswer`` per question are written inside a
    single transaction.  When ``quiz.max_attempts`` is set, the enrolment is
    locked (see ``_lock_enrollment``) before the count is checked, so two
    concurrent (or double-clicked) submissions cannot both slip past the limit;
    a learner who has used every attempt gets ``AttemptsExhaustedError`` and
    nothing is written.  Submitted choice ids are sanitised exactly as in
    ``grade_attempt`` before being stored, so a foreign id is recorded as no
    answer rather than trusted.
    """
    answers = answers_from_post(quiz, post_data)
    questions = list(quiz.questions.prefetch_related("choices"))
    if not questions:
        raise QuizNotGradableError("A quiz with no questions cannot be graded.")

    # The quiz reads above run outside the transaction on purpose: on SQLite a
    # read before the write lock would take a shared lock that must then be
    # upgraded, which can deadlock.  The lock is the first statement inside.
    with transaction.atomic():
        _lock_enrollment(enrollment)
        if quiz.max_attempts is not None:
            used = QuizAttempt.objects.filter(enrollment=enrollment, quiz=quiz).count()
            if used >= quiz.max_attempts:
                raise AttemptsExhaustedError(
                    "This learner has used all attempts for this quiz."
                )

        sanitised, _per_question, score = _evaluate(questions, answers)
        total = len(questions)
        percent, passed = _summarise(quiz, score, total)
        attempt = QuizAttempt.objects.create(
            enrollment=enrollment,
            quiz=quiz,
            score=score,
            total=total,
            percent=percent,
            passed=passed,
        )
        AttemptAnswer.objects.bulk_create(
            [
                AttemptAnswer(
                    attempt=attempt,
                    question=question,
                    selected_choice_id=sanitised[question.pk],
                )
                for question in questions
            ]
        )
    return attempt
