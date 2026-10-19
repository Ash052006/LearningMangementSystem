"""Seed idempotent demo data so the site can be explored straight away.

Run with ``python manage.py seed_demo``.  Safe to run repeatedly: every row is
created through ``get_or_create`` (question attempts are guarded by an
existence check), so nothing is duplicated.  The demo accounts' passwords are
the one thing reset on each run, so the credentials printed below always work.
"""

from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from courses.models import Course, Lesson
from courses.services import enrol, mark_lesson_complete
from quizzes.models import Choice, Question, Quiz
from quizzes.services import submit_attempt

User = get_user_model()

# One password for every demo account, printed at the end of the command.
DEMO_PASSWORD = "demo12345"

PUBLISHED_TITLE = "E-Filing for Court Clerks"
DRAFT_TITLE = "Advanced E-Filing: Case Management"

# (title, content) in teaching order; position is derived from the index.
PUBLISHED_LESSONS = [
    (
        "Why e-filing",
        "E-filing replaces the paper bundle with a single electronic record. "
        "A filing becomes available to every authorised user at once, and the "
        "court keeps one searchable copy instead of many paper ones.",
    ),
    (
        "Logging in and your role",
        "Staff sign in with their court account. A clerk's role decides which "
        "cases and documents they can see, so sign out when you leave a shared "
        "terminal and never share credentials.",
    ),
    (
        "Preparing a document for upload",
        "Save the document as a PDF and check it is readable, correctly named "
        "and within the court's file-size limit. Remove tracked changes and "
        "comments before filing: everything in the file becomes part of the "
        "record.",
    ),
    (
        "Submitting a filing and tracking it",
        "Choose the case, attach the document, confirm the filing details and "
        "submit. The system returns a receipt with a timestamp; use the case "
        "number to track the filing's status.",
    ),
    (
        "Common errors and how to fix them",
        "The most common rejections are a file that is too large, an "
        "unsupported format, or a filing attached to the wrong case. Fix the "
        "document or the case selection and resubmit; the original receipt is "
        "kept for the audit trail.",
    ),
]

DRAFT_LESSONS = [
    (
        "Bulk filing",
        "Draft material: submitting several documents for one case in a "
        "single operation.",
    ),
    (
        "Managing sealed documents",
        "Draft material: restricting access to documents a judge has sealed.",
    ),
]

# (question text, [(choice text, is_correct), ...]) in quiz order.
QUESTIONS = [
    (
        "Which file format is best for a filing that must stay readable and "
        "searchable?",
        [
            ("A PDF", True),
            ("A scanned JPEG photo", False),
            ("A spreadsheet", False),
            ("A ZIP archive of screenshots", False),
        ],
    ),
    (
        "Before uploading a filing, what should a clerk check first?",
        [
            (
                "That the document uses an accepted file type and is within "
                "the size limit",
                True,
            ),
            ("That the document has a decorative cover page", False),
            ("That the filename includes the clerk's initials", False),
            ("That the document is also emailed to the judge", False),
        ],
    ),
    (
        "What does the case number on a filing do?",
        [
            ("Links the document to the correct case record", True),
            ("Sets the filing fee automatically", False),
            ("Chooses the presiding judge", False),
            ("Marks the document as confidential", False),
        ],
    ),
    (
        "A filing is rejected for exceeding the size limit. What is the best "
        "next step?",
        [
            ("Split or compress the document and resubmit", True),
            ("Submit the same document again unchanged", False),
            ("Change the file extension to hide the size", False),
            ("Ask the other party to email it to the court", False),
        ],
    ),
    (
        "Why does the system ask you to confirm a filing before submitting?",
        [
            ("So the filer reviews the document and details one last time", True),
            ("So the system can charge a second fee", False),
            ("So the judge can pre-approve the document", False),
            ("So the document is published on a public website", False),
        ],
    ),
]

# How many of the first questions demo_learner1 answers correctly.
DETAILED_LEARNER_CORRECT = 4


class Command(BaseCommand):
    """Create the demo accounts, courses and a little learner activity."""

    help = "Create idempotent demo data (accounts, courses, quiz, progress)."

    def handle(self, *args: Any, **options: Any) -> None:
        instructor = self._ensure_user("demo_instructor", User.Role.INSTRUCTOR)
        learner1 = self._ensure_user("demo_learner1", User.Role.LEARNER)
        learner2 = self._ensure_user("demo_learner2", User.Role.LEARNER)

        course = self._ensure_course(
            instructor,
            PUBLISHED_TITLE,
            status=Course.Status.PUBLISHED,
            description=(
                "A practical introduction to filing and managing case "
                "documents in the court's e-filing system."
            ),
            enforce_order=True,
        )
        lessons = self._sync_lessons(course, PUBLISHED_LESSONS)
        quiz = self._sync_quiz(course)
        self._sync_questions(quiz)

        draft = self._ensure_course(
            instructor,
            DRAFT_TITLE,
            status=Course.Status.DRAFT,
            description=(
                "Work in progress, kept as a draft so the learner-facing 404 "
                "behaviour can be demonstrated."
            ),
        )
        self._sync_lessons(draft, DRAFT_LESSONS)

        # learner1 has partial progress and one graded attempt; learner2 is
        # enrolled but has done nothing, so the dashboard shows both cases.
        enrollment1 = enrol(learner1, course)
        enrol(learner2, course)
        for lesson in lessons[:2]:
            mark_lesson_complete(enrollment1, lesson)
        if not enrollment1.quiz_attempts.filter(quiz=quiz).exists():
            attempt = submit_attempt(enrollment1, quiz, self._attempt_payload(quiz))
            self.stdout.write(
                f"  demo_learner1 attempt: {attempt.percent}% "
                f"({'passed' if attempt.passed else 'failed'})"
            )

        self._report(course, draft)

    def _ensure_user(self, username: str, role: str) -> User:
        """Return the demo user, creating it with the known password."""
        user, _ = User.objects.get_or_create(
            username=username,
            defaults={
                "role": role,
                "email": f"{username}@example.com",
                "first_name": username.replace("demo_", "").replace("_", " ").title(),
            },
        )
        # Set the role and password every run so the printed credentials are
        # always valid, even if the account was edited by hand.
        user.role = role
        user.set_password(DEMO_PASSWORD)
        user.save()
        return user

    def _ensure_course(
        self,
        instructor: User,
        title: str,
        *,
        status: str,
        description: str,
        enforce_order: bool = False,
    ) -> Course:
        """Return the demo course for ``instructor``, creating it if needed."""
        course, _ = Course.objects.get_or_create(
            instructor=instructor,
            title=title,
            defaults={"status": status},
        )
        course.status = status
        course.description = description
        course.enforce_order = enforce_order
        course.save()
        return course

    def _sync_lessons(
        self, course: Course, lessons: list[tuple[str, str]]
    ) -> list[Lesson]:
        """Create the course's lessons in order and return them."""
        rows = []
        for position, (title, content) in enumerate(lessons, start=1):
            lesson, _ = Lesson.objects.get_or_create(
                course=course,
                position=position,
                defaults={"title": title, "content": content},
            )
            rows.append(lesson)
        return rows

    def _sync_quiz(self, course: Course) -> Quiz:
        """Return the course's quiz, creating it with demo defaults."""
        quiz, _ = Quiz.objects.get_or_create(
            course=course,
            defaults={"pass_mark": 70, "max_attempts": 3, "show_answers": True},
        )
        return quiz

    def _sync_questions(self, quiz: Quiz) -> None:
        """Create the quiz's questions and choices in order."""
        for position, (text, choices) in enumerate(QUESTIONS, start=1):
            question, _ = Question.objects.get_or_create(
                quiz=quiz,
                position=position,
                defaults={"text": text},
            )
            for choice_text, is_correct in choices:
                Choice.objects.get_or_create(
                    question=question,
                    text=choice_text,
                    defaults={"is_correct": is_correct},
                )

    def _attempt_payload(self, quiz: Quiz) -> dict[str, int]:
        """Build a submission dict answering most questions correctly.

        The first ``DETAILED_LEARNER_CORRECT`` questions get the correct
        choice and the rest get a wrong one, so the stored attempt is a
        believable mix rather than a perfect score.
        """
        payload: dict[str, int] = {}
        questions = quiz.questions.prefetch_related("choices")
        for index, question in enumerate(questions):
            choices = list(question.choices.all())
            want_correct = index < DETAILED_LEARNER_CORRECT
            choice = next(c for c in choices if c.is_correct == want_correct)
            payload[f"question_{question.pk}"] = choice.pk
        return payload

    def _report(self, course: Course, draft: Course) -> None:
        """Print the credentials and a short summary of what was created."""
        published_count = len(PUBLISHED_LESSONS)
        draft_count = len(DRAFT_LESSONS)
        self.stdout.write(self.style.SUCCESS("Demo data is ready."))
        self.stdout.write(
            f'  published course: "{course.title}" ({published_count} lessons, quiz)'
        )
        self.stdout.write(
            f'  draft course:     "{draft.title}" '
            f"({draft_count} lessons, 404 to learners)"
        )
        self.stdout.write("")
        self.stdout.write("Log in at http://127.0.0.1:8000/accounts/login/")
        self.stdout.write("")
        self.stdout.write("  role        username         password")
        self.stdout.write("  ----------  ---------------  ------------")
        self.stdout.write(f"  instructor  demo_instructor  {DEMO_PASSWORD}")
        self.stdout.write(f"  learner     demo_learner1    {DEMO_PASSWORD}")
        self.stdout.write(f"  learner     demo_learner2    {DEMO_PASSWORD}")
