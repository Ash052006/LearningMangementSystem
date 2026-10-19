"""URLs for the courses app.

Instructor management pages live under ``manage/`` so the learner-facing
catalogue can keep the clean ``/courses/`` and ``/courses/<pk>/`` paths.
"""

from django.urls import path

from . import views

app_name = "courses"

urlpatterns = [
    # Learner-facing catalogue and flow.
    path("", views.CatalogueView.as_view(), name="catalogue"),
    path("my/", views.MyCoursesView.as_view(), name="my_courses"),
    path(
        "<int:pk>/",
        views.LearnerCourseDetailView.as_view(),
        name="learner_course_detail",
    ),
    path("<int:pk>/enrol/", views.EnrolView.as_view(), name="enrol"),
    path(
        "<int:pk>/lessons/<int:lesson_pk>/",
        views.LearnerLessonView.as_view(),
        name="learner_lesson_detail",
    ),
    path(
        "<int:pk>/lessons/<int:lesson_pk>/complete/",
        views.LessonCompleteView.as_view(),
        name="lesson_complete",
    ),
    path(
        "<int:pk>/lessons/<int:lesson_pk>/attachment/",
        views.LessonAttachmentView.as_view(),
        name="lesson_attachment",
    ),
    # Certificates: POST issues one, GET renders the printable page.
    path(
        "<int:pk>/certificate/",
        views.CertificateView.as_view(),
        name="certificate",
    ),
    path(
        "<int:pk>/certificate/issue/",
        views.CertificateIssueView.as_view(),
        name="certificate_issue",
    ),
    # Instructor course management.
    path(
        "manage/",
        views.InstructorCourseListView.as_view(),
        name="instructor_course_list",
    ),
    path(
        "manage/dashboard/",
        views.InstructorDashboardView.as_view(),
        name="instructor_dashboard",
    ),
    path("manage/new/", views.CourseCreateView.as_view(), name="course_create"),
    path(
        "manage/<int:pk>/",
        views.CourseDetailView.as_view(),
        name="course_detail",
    ),
    path(
        "manage/<int:pk>/dashboard/",
        views.CourseDashboardView.as_view(),
        name="course_dashboard",
    ),
    path(
        "manage/<int:pk>/dashboard/learners.csv",
        views.CourseDashboardCsvView.as_view(),
        name="course_dashboard_csv",
    ),
    path(
        "manage/<int:pk>/edit/",
        views.CourseUpdateView.as_view(),
        name="course_edit",
    ),
    path(
        "manage/<int:pk>/delete/",
        views.CourseDeleteView.as_view(),
        name="course_delete",
    ),
    path(
        "manage/<int:pk>/publish/",
        views.CourseTogglePublishView.as_view(),
        name="course_toggle_publish",
    ),
    # Lessons, nested under a course.
    path(
        "manage/<int:course_pk>/lessons/new/",
        views.LessonCreateView.as_view(),
        name="lesson_create",
    ),
    path(
        "manage/<int:course_pk>/lessons/<int:pk>/edit/",
        views.LessonUpdateView.as_view(),
        name="lesson_edit",
    ),
    path(
        "manage/<int:course_pk>/lessons/<int:pk>/delete/",
        views.LessonDeleteView.as_view(),
        name="lesson_delete",
    ),
    path(
        "manage/<int:course_pk>/lessons/<int:pk>/up/",
        views.LessonMoveUpView.as_view(),
        name="lesson_move_up",
    ),
    path(
        "manage/<int:course_pk>/lessons/<int:pk>/down/",
        views.LessonMoveDownView.as_view(),
        name="lesson_move_down",
    ),
]
