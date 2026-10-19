"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from accounts.views import HomeRedirectView
from courses.views import CertificateVerifyView

urlpatterns = [
    path("", HomeRedirectView.as_view(), name="home"),
    path("accounts/", include("accounts.urls")),
    path("courses/", include("courses.urls")),
    path("quizzes/", include("quizzes.urls")),
    # Public certificate verification: no login, keyed by a random UUID.
    path(
        "certificates/<uuid:code>/",
        CertificateVerifyView.as_view(),
        name="certificate_verify",
    ),
    # JSON API, versioned under /api/v1/.  The schema is served for tooling and
    # Swagger UI renders it at /api/docs/.
    path("api/v1/", include("api.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("admin/", admin.site.urls),
    # Django's built-in language switcher: reads a `language` field in a POST
    # body, sets the cookie and redirects back to `next`.
    path("i18n/", include("django.conf.urls.i18n")),
]

# MEDIA_URL is deliberately not wired up: lesson attachments are private and
# are only ever streamed by courses.views.LessonAttachmentView, which checks
# that the requester owns the course or is enrolled in it.
