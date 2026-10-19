"""Views for the accounts app: signup, login, logout, role-based home.

Login and logout use Django's built-in auth views.  ``LoginView`` already
validates the ``?next=`` parameter with ``url_has_allowed_host_and_scheme``
(and falls back to LOGIN_REDIRECT_URL), so an external ``next`` can never turn
the login form into an open redirect; we only add template and success
message handling here.
"""

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import AuthenticationForm
from django.http import HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.generic import CreateView, RedirectView, TemplateView

from .forms import SignUpForm
from .mixins import InstructorRequiredMixin


class HomeRedirectView(RedirectView):
    """Send each visitor to the landing page for their role.

    Instructors go to their dashboard, logged-in learners to the course
    catalogue, and anonymous visitors to the login page (this is an internal
    staff site).
    """

    def get_redirect_url(self, *args: object, **kwargs: object) -> str:
        user = self.request.user
        if not user.is_authenticated:
            return reverse("accounts:login")
        if user.is_instructor:
            return reverse("accounts:dashboard")
        return reverse("courses:catalogue")


class SignUpView(CreateView):
    """Register a new account, then send the user to the login page."""

    form_class = SignUpForm
    template_name = "accounts/signup.html"

    def form_valid(self, form: SignUpForm) -> HttpResponse:
        response = super().form_valid(form)
        messages.success(
            self.request, _("Your account has been created. Please log in.")
        )
        return response

    def get_success_url(self) -> str:
        return reverse("accounts:login")


class LoginView(auth_views.LoginView):
    """Django's LoginView with our template and a welcome message."""

    template_name = "accounts/login.html"

    def form_valid(self, form: AuthenticationForm) -> HttpResponse:
        messages.success(
            self.request,
            _("Welcome back, %(username)s.")
            % {"username": form.get_user().get_username()},
        )
        return super().form_valid(form)


class DashboardView(InstructorRequiredMixin, TemplateView):
    """The instructor landing page (the courses task fills in its links).

    Instructors only: ``InstructorRequiredMixin`` sends anonymous visitors to
    the login page and answers a logged-in learner with a 403, so this
    staff-only route can no longer be rendered without a role check.
    """

    template_name = "accounts/dashboard.html"
