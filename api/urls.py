"""URLs for the versioned JSON API (mounted under ``/api/v1/``)."""

from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token

from . import views

app_name = "api"

urlpatterns = [
    path("courses/", views.CourseListView.as_view(), name="course-list"),
    path("courses/<int:pk>/", views.CourseDetailView.as_view(), name="course-detail"),
    path("courses/<int:pk>/enrol/", views.EnrolView.as_view(), name="course-enrol"),
    path(
        "courses/<int:pk>/lessons/<int:lesson_pk>/complete/",
        views.LessonCompleteView.as_view(),
        name="lesson-complete",
    ),
    path("my/enrolments/", views.MyEnrolmentsView.as_view(), name="my-enrolments"),
    # Exchange username/password for a token; the endpoint is the token-auth
    # entry point for scripts.
    path("auth/token/", obtain_auth_token, name="token"),
]
