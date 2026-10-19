"""Admin registration for quizzes.

Quiz/Question/Choice are editable; QuizAttempt and AttemptAnswer are
read-only (immutable assessment records): view permissions stay enabled so
staff can inspect them, but add/change/delete are refused on the server.
"""

from django.contrib import admin
from django.http import HttpRequest

from .models import AttemptAnswer, Choice, Question, Quiz, QuizAttempt


class QuestionInline(admin.TabularInline):
    """Questions shown on the quiz page, ordered by position."""

    model = Question
    extra = 0
    fields = ["position", "text"]
    ordering = ["position"]
    # Needs Question to be registered on its own too (it is, below).
    show_change_link = True


class ChoiceInline(admin.TabularInline):
    """Answer options shown on the question page."""

    model = Choice
    extra = 0
    fields = ["text", "is_correct"]


@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    """Quiz settings with its questions inline."""

    list_display = ["course", "pass_mark", "max_attempts", "show_answers"]
    list_filter = ["show_answers"]
    search_fields = ["course__title"]
    list_select_related = ["course"]
    inlines = [QuestionInline]


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    """Questions with their choices inline."""

    list_display = ["quiz", "position", "text"]
    list_filter = ["quiz__course"]
    search_fields = ["text", "quiz__course__title"]
    list_select_related = ["quiz__course"]
    ordering = ["quiz", "position"]
    inlines = [ChoiceInline]


class ReadOnlyAdmin(admin.ModelAdmin):
    """Shows records but refuses every mutating action.

    Django's view permission keeps the changelist and a read-only detail page
    accessible while add/change/delete return False server-side.
    """

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False


@admin.register(QuizAttempt)
class QuizAttemptAdmin(ReadOnlyAdmin):
    """Immutable attempt records: list, filter and inspect only."""

    list_display = [
        "enrollment",
        "quiz",
        "score",
        "total",
        "percent",
        "passed",
        "submitted_at",
    ]
    list_filter = ["passed", "quiz__course"]
    search_fields = ["enrollment__user__username", "quiz__course__title"]
    date_hierarchy = "submitted_at"
    list_select_related = [
        "enrollment__user",
        "enrollment__course",
        "quiz",
        "quiz__course",
    ]


@admin.register(AttemptAnswer)
class AttemptAnswerAdmin(ReadOnlyAdmin):
    """Immutable per-question answers: inspect only."""

    list_display = ["attempt", "question", "selected_choice"]
    list_filter = ["question__quiz__course"]
    search_fields = ["question__text", "attempt__enrollment__user__username"]
    list_select_related = ["attempt__enrollment__user", "question", "selected_choice"]
