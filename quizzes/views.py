"""Views for quiz authoring and quiz taking.

Authoring: the single editor page wires together three pieces: the quiz
settings form, a question inline formset and, nested inside every question, a
choice inline formset.  Reordering and deleting questions are separate
POST-only endpoints, so a question that already has attempts can be protected
with a clear message rather than failing halfway through a big form save.
Access is delegated to ``accounts.mixins.OwnerCourseMixin``: anonymous users
are sent to login, non-instructors get a 403 and another instructor's course
raises a 404.

Taking: enrolled learners answer the quiz (POST is graded server-side, never
in the browser) and see a result, their attempt history and, when the quiz
allows it, an answer review.  Instructors can run the same flow on their own
course as a preview, which grades but saves nothing.
"""

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views import View

from accounts.mixins import OwnerCourseMixin
from courses.models import Course, Enrollment

from .forms import CHOICE_SLOTS, ChoiceFormSet, QuestionFormSet, QuizForm
from .models import Question, Quiz, QuizAttempt
from .services import (
    AttemptsExhaustedError,
    GradeResult,
    QuizNotGradableError,
    answers_from_post,
    grade_attempt,
    move_question_down,
    move_question_up,
    question_has_attempts,
    resequence_questions,
    submit_attempt,
)


class QuizEditorView(OwnerCourseMixin, View):
    """Create or edit the quiz of a course the instructor owns."""

    template_name = "quizzes/quiz_edit.html"

    def get_course(self) -> Course:
        """The course named in the URL, scoped to the logged-in instructor."""
        return get_object_or_404(
            Course, pk=self.kwargs["course_pk"], instructor=self.request.user
        )

    def _choice_pairs(self, question_formset: Any, data: Any) -> list[tuple[Any, Any]]:
        """Pair each question form with the choice formset for that question.

        A choice formset is an ordinary inline formset under the (possibly
        unsaved) question; for a GET it renders ``CHOICE_SLOTS`` rows in total
        (existing choices plus blank ones), and for a POST it is bound to the
        matching names in ``data``.
        """
        pairs = []
        for question_form in question_formset.forms:
            prefix = f"choices-{question_form.prefix}"
            choice_formset = ChoiceFormSet(
                data, instance=question_form.instance, prefix=prefix
            )
            # ``extra`` is only read when rendering an unbound formset, so this
            # fills each question up to a fixed number of visible slots.
            choice_formset.extra = max(
                0, CHOICE_SLOTS - choice_formset.initial_form_count()
            )
            pairs.append((question_form, choice_formset))
        return pairs

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        quiz = Quiz.objects.filter(course=course).first()
        # An unsaved Quiz instance lets the form render before the quiz exists.
        instance = quiz or Quiz(course=course)
        quiz_form = QuizForm(instance=instance)
        question_formset = QuestionFormSet(instance=instance)
        pairs = self._choice_pairs(question_formset, None)
        return self._render(request, course, quiz, quiz_form, question_formset, pairs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        quiz = Quiz.objects.filter(course=course).first()
        instance = quiz or Quiz(course=course)
        quiz_form = QuizForm(request.POST, instance=instance)
        question_formset = QuestionFormSet(request.POST, instance=instance)
        pairs = self._choice_pairs(question_formset, request.POST)

        quiz_valid = quiz_form.is_valid()
        questions_valid = question_formset.is_valid()
        choices_valid = True
        for question_form, choice_formset in pairs:
            choices_changed = any(form.has_changed() for form in choice_formset.forms)
            # A completely untouched "add another" row (no question text and
            # no choices) is not a question yet; ignore it.
            active = (
                question_form.instance.pk is not None
                or question_form.has_changed()
                or choices_changed
            )
            if not active:
                continue
            if not choice_formset.is_valid():
                choices_valid = False
            # The formset treats an empty extra row as "unchanged" and skips
            # it, so enforce question text here when choices were supplied.
            question_valid = question_form.is_valid()
            text = (question_form.cleaned_data or {}).get("text", "")
            if question_valid and not text.strip():
                question_form.add_error("text", _("Enter the question text."))
                question_valid = False
            if not question_valid:
                questions_valid = False

        if quiz_valid and questions_valid and choices_valid:
            quiz = quiz_form.save()
            question_formset.save()
            for question_form, choice_formset in pairs:
                # Empty rows were not saved, so they still have no primary key.
                if question_form.instance.pk is not None:
                    choice_formset.save()
            resequence_questions(quiz)
            messages.success(
                request, _('Quiz for "%(title)s" saved.') % {"title": course.title}
            )
            return redirect("quizzes:quiz_edit", course_pk=course.pk)

        messages.error(request, _("Please correct the errors below."))
        return self._render(request, course, quiz, quiz_form, question_formset, pairs)

    def _render(
        self,
        request: HttpRequest,
        course: Course,
        quiz: Quiz | None,
        quiz_form: QuizForm,
        question_formset: Any,
        pairs: list[tuple[Any, Any]],
    ) -> HttpResponse:
        context = {
            "course": course,
            "quiz": quiz,
            "quiz_form": quiz_form,
            "question_formset": question_formset,
            "choice_pairs": pairs,
            # Warn about editing a quiz that already has assessment history.
            "has_attempts": bool(quiz and quiz.attempts.exists()),
        }
        return render(request, self.template_name, context)


class QuestionMoveView(OwnerCourseMixin, View):
    """POST-only base view: move a question one place in its quiz."""

    direction = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = get_object_or_404(
            Course, pk=kwargs["course_pk"], instructor=request.user
        )
        question = get_object_or_404(Question, pk=kwargs["pk"], quiz__course=course)
        mover = move_question_up if self.direction == "up" else move_question_down
        if mover(question):
            messages.success(request, _("Question order updated."))
        else:
            messages.info(
                request,
                _("That question is already at the %(edge)s.")
                % {"edge": _("top") if self.direction == "up" else _("bottom")},
            )
        return redirect("quizzes:quiz_edit", course_pk=course.pk)


class QuestionMoveUpView(QuestionMoveView):
    """POST-only: swap a question with the one above it."""

    direction = "up"


class QuestionMoveDownView(QuestionMoveView):
    """POST-only: swap a question with the one below it."""

    direction = "down"


class QuestionDeleteView(OwnerCourseMixin, View):
    """POST-only: delete a question, unless learners have already answered it.

    Hard-deleting an answered question would destroy assessment history
    (``AttemptAnswer.question`` is ``PROTECT``), so instead of deleting we keep
    the question and explain why.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = get_object_or_404(
            Course, pk=kwargs["course_pk"], instructor=request.user
        )
        question = get_object_or_404(Question, pk=kwargs["pk"], quiz__course=course)
        if question_has_attempts(question):
            messages.error(
                request,
                _(
                    "This question cannot be deleted because learners have "
                    "already answered it."
                ),
            )
        else:
            quiz = question.quiz
            question.delete()
            resequence_questions(quiz)
            messages.success(request, _("Question deleted."))
        return redirect("quizzes:quiz_edit", course_pk=course.pk)


class QuizLearnerMixin(LoginRequiredMixin):
    """Resolve the course, quiz and enrolment for a learner taking a quiz.

    Unlike the lesson flow (where an enrolled learner keeps access to a course
    that was later withdrawn), the quiz is only available while its course is
    published: a draft course is a 404.  An authenticated user who is not
    enrolled gets a 403 on both GET and POST.
    """

    def get_course(self) -> Course:
        """The published course named in the URL (draft courses 404)."""
        return get_object_or_404(
            Course, pk=self.kwargs["course_pk"], status=Course.Status.PUBLISHED
        )

    def get_enrollment(self, course: Course) -> Enrollment:
        """The learner's enrolment in ``course``, or a 403."""
        enrollment = Enrollment.objects.filter(
            user=self.request.user, course=course
        ).first()
        if enrollment is None:
            raise PermissionDenied
        return enrollment

    def get_quiz(self, course: Course) -> Quiz:
        """The course's quiz, but only when it has questions.

        A quiz with no questions is never shown to learners, so it is a 404
        here even though the course (and possibly an old attempt) exists.
        """
        quiz = Quiz.objects.filter(course=course).first()
        if quiz is None or not quiz.has_questions:
            raise Http404("This quiz is not available.")
        return quiz


def _result_from_attempt(attempt: QuizAttempt) -> GradeResult:
    """Reuse the stored, immutable result of a saved attempt."""
    return GradeResult(
        score=attempt.score,
        total=attempt.total,
        percent=attempt.percent,
        passed=attempt.passed,
        per_question={},
    )


def _review_row(question: Question, selected: Any, correct: Any) -> dict[str, Any]:
    """One answer-review row: what was chosen, and what was correct."""
    return {
        "question": question,
        "selected": selected,
        "correct_choice": correct,
        "is_correct": (
            selected is not None and correct is not None and selected.pk == correct.pk
        ),
    }


def _review_from_attempt(attempt: QuizAttempt, quiz: Quiz) -> list[dict[str, Any]]:
    """Build the answer review from stored answers plus the current quiz."""
    answered = {
        answer.question_id: answer
        for answer in attempt.answers.select_related("selected_choice")
    }
    review = []
    for question in quiz.questions.prefetch_related("choices"):
        choices = list(question.choices.all())
        answer = answered.get(question.pk)
        selected = answer.selected_choice if answer is not None else None
        correct = next((choice for choice in choices if choice.is_correct), None)
        review.append(_review_row(question, selected, correct))
    return review


def _review_from_answers(
    quiz: Quiz, answers: dict[int, int | None], per_question: dict[int, bool]
) -> list[dict[str, Any]]:
    """Build an answer review for unsaved (preview) answers.

    Correctness comes from the grading result, so a tampered or foreign choice
    id stays wrong even though the row is only for display.
    """
    review = []
    for question in quiz.questions.prefetch_related("choices"):
        choices = list(question.choices.all())
        raw = answers.get(question.pk)
        selected = next((choice for choice in choices if choice.pk == raw), None)
        correct = next((choice for choice in choices if choice.is_correct), None)
        row = _review_row(question, selected, correct)
        row["is_correct"] = per_question.get(question.pk, False)
        review.append(row)
    return review


def _remaining_attempts(quiz: Quiz, enrollment: Enrollment) -> int | None:
    """Attempts left for this quiz, or ``None`` when attempts are unlimited."""
    if quiz.max_attempts is None:
        return None
    used = enrollment.quiz_attempts.filter(quiz=quiz).count()
    return max(0, quiz.max_attempts - used)


class TakeQuizView(QuizLearnerMixin, View):
    """GET shows the unanswered quiz; POST grades it (server-side)."""

    template_name = "quizzes/take_quiz.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        enrollment = self.get_enrollment(course)
        quiz = self.get_quiz(course)
        if _remaining_attempts(quiz, enrollment) == 0:
            messages.info(request, _("You have no attempts left for this quiz."))
            latest = enrollment.quiz_attempts.filter(quiz=quiz).first()
            return self._redirect_to_latest(course, latest)
        return render(request, self.template_name, self._context(course, quiz))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        enrollment = self.get_enrollment(course)
        quiz = self.get_quiz(course)
        try:
            attempt = submit_attempt(enrollment, quiz, request.POST)
        except AttemptsExhaustedError:
            messages.error(
                request, _("You have used all of your attempts for this quiz.")
            )
            latest = enrollment.quiz_attempts.filter(quiz=quiz).first()
            return self._redirect_to_latest(course, latest)
        except QuizNotGradableError as exc:
            raise Http404(str(exc)) from exc
        messages.success(request, _("Your answers have been submitted."))
        # POST/redirect/GET: refreshing the result cannot resubmit the quiz.
        return redirect("quizzes:result", course_pk=course.pk, pk=attempt.pk)

    def _context(self, course: Course, quiz: Quiz) -> dict[str, Any]:
        return {
            "course": course,
            "quiz": quiz,
            "questions": list(quiz.questions.prefetch_related("choices")),
            "preview": False,
        }

    def _redirect_to_latest(
        self, course: Course, attempt: QuizAttempt | None
    ) -> HttpResponse:
        if attempt is not None:
            return redirect("quizzes:result", course_pk=course.pk, pk=attempt.pk)
        return redirect("courses:learner_course_detail", pk=course.pk)


class QuizResultView(QuizLearnerMixin, View):
    """One saved attempt's result, history and (when allowed) review."""

    template_name = "quizzes/quiz_result.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        enrollment = self.get_enrollment(course)
        quiz = self.get_quiz(course)
        attempt = get_object_or_404(
            QuizAttempt, pk=kwargs["pk"], enrollment=enrollment, quiz=quiz
        )
        remaining = _remaining_attempts(quiz, enrollment)
        context = {
            "course": course,
            "quiz": quiz,
            "attempt": attempt,
            "result": _result_from_attempt(attempt),
            "history": list(enrollment.quiz_attempts.filter(quiz=quiz)),
            # The review is only shown when the instructor allows it.
            "review": _review_from_attempt(attempt, quiz)
            if quiz.show_answers
            else None,
            "remaining": remaining,
            "can_retake": remaining is None or remaining > 0,
            "preview": False,
        }
        return render(request, self.template_name, context)


class QuizPreviewView(OwnerCourseMixin, View):
    """Instructor preview of their own quiz: it grades but saves nothing."""

    def get_course(self) -> Course:
        return get_object_or_404(
            Course, pk=self.kwargs["course_pk"], instructor=self.request.user
        )

    def _quiz(self, course: Course) -> Quiz:
        quiz = Quiz.objects.filter(course=course).first()
        if quiz is None or not quiz.has_questions:
            raise Http404("This quiz has no questions to preview.")
        return quiz

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        quiz = self._quiz(course)
        context = {
            "course": course,
            "quiz": quiz,
            "questions": list(quiz.questions.prefetch_related("choices")),
            "preview": True,
        }
        return render(request, "quizzes/take_quiz.html", context)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        quiz = self._quiz(course)
        answers = answers_from_post(quiz, request.POST)
        try:
            result = grade_attempt(quiz, answers)
        except QuizNotGradableError as exc:
            raise Http404(str(exc)) from exc
        context = {
            "course": course,
            "quiz": quiz,
            "attempt": None,
            "result": result,
            "history": None,
            "review": _review_from_answers(quiz, answers, result.per_question),
            "remaining": None,
            "can_retake": False,
            "preview": True,
        }
        return render(request, "quizzes/quiz_result.html", context)
