"""URLs for the quizzes app.

Every route is nested under the course it belongs to, so ownership (for
instructor routes) or enrolment (for learner routes) can be checked from
``course_pk`` alone.  Instructor management lives under ``manage/`` and the
learner flow under ``course/`` so the two can never be confused.
"""

from django.urls import path

from . import views

app_name = "quizzes"

urlpatterns = [
    # Learner-facing: take the quiz and review a saved attempt.
    path(
        "course/<int:course_pk>/",
        views.TakeQuizView.as_view(),
        name="take",
    ),
    path(
        "course/<int:course_pk>/attempts/<int:pk>/",
        views.QuizResultView.as_view(),
        name="result",
    ),
    # Instructor-facing: authoring, ordering and a save-nothing preview.
    path(
        "manage/<int:course_pk>/",
        views.QuizEditorView.as_view(),
        name="quiz_edit",
    ),
    path(
        "manage/<int:course_pk>/preview/",
        views.QuizPreviewView.as_view(),
        name="preview",
    ),
    path(
        "manage/<int:course_pk>/questions/<int:pk>/up/",
        views.QuestionMoveUpView.as_view(),
        name="question_move_up",
    ),
    path(
        "manage/<int:course_pk>/questions/<int:pk>/down/",
        views.QuestionMoveDownView.as_view(),
        name="question_move_down",
    ),
    path(
        "manage/<int:course_pk>/questions/<int:pk>/delete/",
        views.QuestionDeleteView.as_view(),
        name="question_delete",
    ),
]
