"""Reusable view mixins for role- and ownership-based access control.

Kept in the accounts app because they describe the *user* (role, ownership of
their own data) rather than any single feature area.
"""

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

from courses.models import Course


class InstructorRequiredMixin(LoginRequiredMixin):
    """Allow instructors only.

    Anonymous visitors are redirected to the login page (handled by
    ``LoginRequiredMixin``); authenticated non-instructors get a 403 rather
    than a redirect, so a learner cannot reach a staff-only page even by
    logging in.
    """

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated and not request.user.is_instructor:
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class OwnerCourseMixin(InstructorRequiredMixin):
    """Restrict a view to courses created by the logged-in instructor.

    ``get_queryset`` scopes every lookup to the request user, so a detail,
    update, delete or publish attempt against someone else's course raises
    ``Http404`` (``get_object`` uses this queryset).  Returning 404 rather than
    403 avoids confirming that another instructor's course exists.
    """

    def get_queryset(self) -> QuerySet[Course]:
        return Course.objects.filter(instructor=self.request.user)
