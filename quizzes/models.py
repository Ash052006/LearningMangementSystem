"""Models for the quizzes app: one end-of-course quiz per course.

Attempts are immutable: a QuizAttempt and its AttemptAnswer rows are created
together (atomically, in a later service) and never updated afterwards.
"""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from courses.models import Course, Enrollment


class Quiz(models.Model):
    """The multiple-choice quiz attached to a course."""

    course = models.OneToOneField(
        Course,
        on_delete=models.CASCADE,
        related_name="quiz",
        verbose_name=_("course"),
    )
    pass_mark = models.PositiveIntegerField(
        _("pass mark"),
        default=70,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=_("Minimum percentage required to pass."),
    )
    # NULL means unlimited attempts.
    max_attempts = models.PositiveIntegerField(
        _("maximum attempts"),
        null=True,
        blank=True,
        help_text=_("Leave blank for unlimited attempts."),
    )
    show_answers = models.BooleanField(
        _("show answers"),
        default=True,
        help_text=_("Show the correct answers to learners after each attempt."),
    )

    class Meta:
        verbose_name = _("quiz")
        verbose_name_plural = _("quizzes")

    def __str__(self) -> str:
        return f"{_('Quiz')}: {self.course.title}"

    @property
    def has_questions(self) -> bool:
        """True when the quiz has at least one question.

        A quiz with no questions is not shown to learners, even when its
        course is published; this property is the single place that rule is
        expressed.
        """
        return self.questions.exists()


class Question(models.Model):
    """A single question inside a quiz."""

    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name="questions",
        verbose_name=_("quiz"),
    )
    text = models.TextField(_("text"))
    position = models.PositiveIntegerField(_("position"))

    class Meta:
        ordering = ["position"]
        verbose_name = _("question")
        verbose_name_plural = _("questions")

    def __str__(self) -> str:
        return f"Q{self.position}: {self.text[:60]}"


class Choice(models.Model):
    """One answer option of a question; at most one correct per question."""

    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name="choices",
        verbose_name=_("question"),
    )
    text = models.CharField(_("text"), max_length=200)
    is_correct = models.BooleanField(_("is correct"), default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["question"],
                condition=models.Q(is_correct=True),
                name="unique_correct_per_question",
            ),
        ]
        verbose_name = _("choice")
        verbose_name_plural = _("choices")

    def __str__(self) -> str:
        return self.text


class QuizAttempt(models.Model):
    """An immutable record of one quiz submission by an enrolled learner."""

    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.CASCADE,
        related_name="quiz_attempts",
        verbose_name=_("enrollment"),
    )
    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name="attempts",
        verbose_name=_("quiz"),
    )
    score = models.PositiveIntegerField(_("score"))
    total = models.PositiveIntegerField(_("total"))
    percent = models.DecimalField(
        _("percent"),
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    passed = models.BooleanField(_("passed"), default=False)
    submitted_at = models.DateTimeField(_("submitted at"), auto_now_add=True)

    class Meta:
        ordering = ["-submitted_at"]
        verbose_name = _("quiz attempt")
        verbose_name_plural = _("quiz attempts")

    def __str__(self) -> str:
        return f"{self.enrollment.user} — {self.percent}%"


class AttemptAnswer(models.Model):
    """One question's outcome inside an attempt; never updated after save."""

    attempt = models.ForeignKey(
        QuizAttempt,
        on_delete=models.CASCADE,
        related_name="answers",
        verbose_name=_("attempt"),
    )
    # PROTECT: an answered question is part of the assessment record, so it
    # cannot be deleted while answers still reference it.
    question = models.ForeignKey(
        Question,
        on_delete=models.PROTECT,
        related_name="answers",
        verbose_name=_("question"),
    )
    # SET_NULL: if an option is removed while editing a quiz, the answer row
    # survives with no selection; the score stored on the attempt stays
    # authoritative.
    selected_choice = models.ForeignKey(
        Choice,
        on_delete=models.SET_NULL,
        related_name="selected_in",
        null=True,
        blank=True,
        verbose_name=_("selected choice"),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["attempt", "question"],
                name="unique_attempt_question",
            ),
        ]
        verbose_name = _("attempt answer")
        verbose_name_plural = _("attempt answers")

    def __str__(self) -> str:
        selected = self.selected_choice or _("no answer")
        return f"{self.question} → {selected}"
