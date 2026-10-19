"""Business logic for courses: publishing, lesson ordering and learner progress.

Views stay thin: they validate access, call one of these functions, and turn
the outcome into a message.  Keeping the rules here (rather than in the views)
makes them reusable and directly testable.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q, QuerySet

from accounts.models import User
from quizzes.models import Question, Quiz, QuizAttempt

from .models import Certificate, Course, Enrollment, Lesson, LessonProgress


class CourseNotPublishableError(Exception):
    """Raised when a course with no lessons is asked to publish."""


@dataclass(frozen=True)
class LearnerStat:
    """One row of the per-course learner table.

    A plain value object: the dashboard view renders it and the CSV export
    writes it out, so both stay in step with one definition.
    """

    username: str
    enrolled_at: datetime
    lessons_completed: int
    total_lessons: int
    percent: int
    best_quiz_percent: Decimal | None
    passed: bool
    attempts_count: int


@dataclass(frozen=True)
class QuestionStat:
    """One row of the per-question analytics table."""

    question: Question
    answers_count: int
    correct_count: int
    percent: int


def published_courses() -> QuerySet[Course]:
    """Base queryset for learner-facing listings: published courses only.

    Shared by the catalogue view and the API list endpoint so neither can
    accidentally surface a draft.
    """
    return Course.objects.filter(status=Course.Status.PUBLISHED)


def toggle_course_publish(course: Course) -> bool:
    """Flip a course between draft and published.

    Returns ``True`` when the course ends up published.  Publishing requires at
    least one lesson; raising ``CourseNotPublishableError`` leaves the course
    untouched so the caller can show a clear message.  Unpublishing always
    succeeds.
    """
    if course.is_published:
        course.status = Course.Status.DRAFT
        course.save()
        return False
    if not course.publishable:
        raise CourseNotPublishableError
    course.status = Course.Status.PUBLISHED
    course.save()
    return True


def _apply_positions(lessons: list[Lesson]) -> None:
    """Rewrite ``lessons`` to positions 1..n in the given order.

    Runs in one transaction and moves every row past the current highest
    position first (then to its final value).  The ``(course, position)``
    unique constraint therefore never sees a duplicate mid-update, which a
    naive pairwise swap could not guarantee.
    """
    base = max(lesson.position for lesson in lessons)
    with transaction.atomic():
        for offset, lesson in enumerate(lessons):
            Lesson.objects.filter(pk=lesson.pk).update(position=base + offset + 1)
        for position, lesson in enumerate(lessons, start=1):
            Lesson.objects.filter(pk=lesson.pk).update(position=position)


def resequence_lessons(course: Course) -> None:
    """Compact a course's lesson positions to 1..n, keeping the current order.

    Called after a deletion so the remaining lessons have no gaps (and no
    duplicate positions to collide with the next insert).
    """
    lessons = list(course.lessons.order_by("position", "pk"))
    if lessons:
        _apply_positions(lessons)


def move_lesson(lesson: Lesson, offset: int) -> bool:
    """Move ``lesson`` one place up (offset -1) or down (+1).

    Returns ``False`` when the lesson is already at the edge and nothing
    changes; otherwise swaps it with its neighbour and renumbers the course.
    """
    lessons = list(lesson.course.lessons.order_by("position", "pk"))
    index = lessons.index(lesson)
    target = index + offset
    if not 0 <= target < len(lessons):
        return False
    lessons[index], lessons[target] = lessons[target], lessons[index]
    _apply_positions(lessons)
    return True


def move_lesson_up(lesson: Lesson) -> bool:
    """Move a lesson one position towards the start of its course."""
    return move_lesson(lesson, -1)


def move_lesson_down(lesson: Lesson) -> bool:
    """Move a lesson one position towards the end of its course."""
    return move_lesson(lesson, 1)


def enrol(user: User, course: Course) -> Enrollment:
    """Enrol ``user`` in ``course``, idempotently, and return the enrolment.

    Re-enrolling returns the existing row.  ``get_or_create`` covers the normal
    path; if two requests race between its SELECT and INSERT, the unique
    ``(user, course)`` constraint raises ``IntegrityError``, which we absorb by
    fetching the row the other request created.
    """
    try:
        with transaction.atomic():
            enrollment, _ = Enrollment.objects.get_or_create(user=user, course=course)
    except IntegrityError:
        enrollment = Enrollment.objects.get(user=user, course=course)
    return enrollment


def mark_lesson_complete(enrollment: Enrollment, lesson: Lesson) -> bool:
    """Record ``lesson`` as complete for ``enrollment``, idempotently.

    Returns ``True`` when a new progress row was created and ``False`` when the
    lesson was already complete, so callers can tell the two apart without
    counting rows themselves.

    Callers must check ``can_access_lesson`` first: this function trusts its
    inputs and does not re-check the course's ordering rule.
    """
    _, created = LessonProgress.objects.get_or_create(
        enrollment=enrollment, lesson=lesson
    )
    return created


def unlocked_lesson_ids(
    enrollment: Enrollment,
    completed_lesson_ids: set[int] | None = None,
    lessons: list[Lesson] | None = None,
) -> set[int]:
    """Return the ids of the lessons this enrolment may open.

    With ``enforce_order`` off, every lesson is open.  With it on, a lesson is
    open when the learner has already completed it (so their history stays
    readable) or when every lesson before it is complete.  That means the first
    incomplete lesson is open and everything after it is locked — exactly the
    "do them in order" rule.

    ``completed_lesson_ids`` and ``lessons`` may be passed by a caller that has
    already loaded them (the course outline does), so nothing extra is queried.
    """
    if lessons is None:
        lessons = list(enrollment.course.lessons.all())  # ordered by position
    if not enrollment.course.enforce_order:
        return {lesson.pk for lesson in lessons}
    if completed_lesson_ids is None:
        completed_lesson_ids = set(
            enrollment.lesson_progress.values_list("lesson_id", flat=True)
        )
    unlocked: set[int] = set()
    locked = False
    for lesson in lessons:
        if lesson.pk in completed_lesson_ids or not locked:
            unlocked.add(lesson.pk)
        if lesson.pk not in completed_lesson_ids:
            locked = True
    return unlocked


def can_access_lesson(enrollment: Enrollment, lesson: Lesson) -> bool:
    """True when ``enrollment`` is allowed to open ``lesson``.

    This is the one place the ordering rule is expressed; the lesson view and
    the mark-complete POST both call it, so they can never disagree.  See
    ``unlocked_lesson_ids`` for the rule itself.
    """
    return lesson.pk in unlocked_lesson_ids(enrollment)


def first_blocking_lesson(enrollment: Enrollment, lesson: Lesson) -> Lesson | None:
    """Return the lesson that must be completed to unlock ``lesson``.

    The earliest incomplete lesson with a lower position, or ``None`` when
    nothing blocks it (ordering is off, or every earlier lesson is complete).
    Used to tell the learner *why* a lesson is locked.
    """
    completed = set(enrollment.lesson_progress.values_list("lesson_id", flat=True))
    return next(
        (
            prior
            for prior in lesson.course.lessons.filter(position__lt=lesson.position)
            if prior.pk not in completed
        ),
        None,
    )


def quiz_requirement_met(enrollment: Enrollment) -> bool:
    """True when the enrolment needs no further quiz work.

    A course with no quiz qualifies, and so does a quiz with no questions
    (learners never see such a quiz, so it cannot be a requirement).  When a
    quiz with questions exists, at least one passing attempt is needed.
    """
    try:
        quiz = enrollment.course.quiz
    except Quiz.DoesNotExist:
        return True
    if not quiz.has_questions:
        return True
    return QuizAttempt.objects.filter(
        enrollment=enrollment, quiz=quiz, passed=True
    ).exists()


def certificate_eligible(enrollment: Enrollment) -> bool:
    """True when ``enrollment`` has met every certificate requirement.

    All lessons must be complete — a course with no lessons is never eligible,
    so an empty course cannot hand out instant certificates — and the quiz must
    be passed unless the course has no quiz (or its quiz has no questions).
    """
    total = enrollment.course.lessons.count()
    if total == 0 or enrollment.lesson_progress.count() < total:
        return False
    return quiz_requirement_met(enrollment)


def issue_certificate_if_eligible(enrollment: Enrollment) -> Certificate | None:
    """Issue and return the enrolment's certificate, if it is eligible now.

    Idempotent: the ``OneToOneField`` means a second call returns the existing
    row rather than creating another.  Returns ``None`` when the requirements
    are not met, so the caller can explain what is still missing.

    Eligibility is only checked here, when the certificate is first issued.
    Once a row exists it is never withdrawn, so a certificate stays valid even
    if the instructor later adds a lesson to the course.
    """
    if not certificate_eligible(enrollment):
        return None
    try:
        with transaction.atomic():
            certificate, _ = Certificate.objects.get_or_create(enrollment=enrollment)
    except IntegrityError:
        # Two requests raced between the SELECT and the INSERT; the unique
        # enrolment means the other request created the same row.
        certificate = Certificate.objects.get(enrollment=enrollment)
    return certificate


def progress_percent(completed: int, total: int) -> int:
    """Whole-number completion percentage; 0% for a course with no lessons."""
    return round(completed / total * 100) if total else 0


def course_progress(enrollment: Enrollment) -> dict[str, int]:
    """Return ``completed``, ``total`` and ``percent`` for an enrolment.

    Progress is derived from ``LessonProgress`` rows, never stored.  Deleting a
    lesson also deletes its progress (CASCADE), so ``completed`` can never
    exceed ``total``.
    """
    total = enrollment.course.lessons.count()
    completed = enrollment.lesson_progress.count()
    return {
        "completed": completed,
        "total": total,
        "percent": progress_percent(completed, total),
    }


def enrollments_with_progress(user: User) -> QuerySet[Enrollment]:
    """The user's enrolments with lesson counts annotated for progress.

    ``total_lessons`` and ``completed_lessons`` are counted in the database
    (``distinct=True`` so the join does not multiply rows), so listing a page
    of enrolments stays a constant number of queries.  Shared by the "my
    courses" page and the API's enrolment list.
    """
    return (
        Enrollment.objects.filter(user=user)
        .select_related("course", "course__instructor")
        .annotate(
            total_lessons=Count("course__lessons", distinct=True),
            completed_lessons=Count("lesson_progress", distinct=True),
        )
        .order_by("-enrolled_at")
    )


def course_learner_stats(course: Course) -> list[LearnerStat]:
    """One stats row per enrolled learner, built from a single query.

    Lessons, completed lessons and quiz attempts are all joined onto the
    enrolment, so the row count would be the product of those joins.  The
    counts are therefore aggregated with ``distinct=True`` and the best score
    with ``Max`` (a duplicate row never changes a maximum), which keeps every
    figure correct no matter how many lessons or attempts exist.
    ``select_related("user")`` brings each learner back in the same query, so
    the query count does not grow with the number of learners.
    """
    rows = (
        Enrollment.objects.filter(course=course)
        .select_related("user")
        .annotate(
            total_lessons=Count("course__lessons", distinct=True),
            lessons_completed=Count("lesson_progress", distinct=True),
            attempts_count=Count("quiz_attempts", distinct=True),
            best_quiz_percent=Max("quiz_attempts__percent"),
            any_passed=Max("quiz_attempts__passed"),
        )
        .order_by("user__username")
    )
    return [
        LearnerStat(
            username=row.user.get_username(),
            enrolled_at=row.enrolled_at,
            lessons_completed=row.lessons_completed,
            total_lessons=row.total_lessons,
            percent=progress_percent(row.lessons_completed, row.total_lessons),
            best_quiz_percent=row.best_quiz_percent,
            passed=bool(row.any_passed),
            attempts_count=row.attempts_count,
        )
        for row in rows
    ]


def course_question_stats(course: Course) -> list[QuestionStat]:
    """Percent-correct for every question in ``course``'s quiz.

    Returns an empty list when the course has no quiz.  Each question's answers
    are counted with ``distinct=True`` and the correct ones with a filtered
    ``Count``, so several attempts by the same learner are all counted without
    multiplying each other.
    """
    quiz = Quiz.objects.filter(course=course).first()
    if quiz is None:
        return []
    questions = (
        Question.objects.filter(quiz=quiz)
        .annotate(
            answers_count=Count("answers", distinct=True),
            correct_count=Count(
                "answers",
                filter=Q(answers__selected_choice__is_correct=True),
                distinct=True,
            ),
        )
        .order_by("position")
    )
    return [
        QuestionStat(
            question=question,
            answers_count=question.answers_count,
            correct_count=question.correct_count,
            percent=progress_percent(question.correct_count, question.answers_count),
        )
        for question in questions
    ]
