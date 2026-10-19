"""Tests for accounts: the custom user model and role-based home routing.

Model-test fixtures here and in other apps omit passwords on purpose:
`force_login` does not verify them, and hashing would dominate runtime.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse

User = get_user_model()

FORBIDDEN = 403
NOT_FOUND = 404

# Real password hashing is deliberately slow; these tests write passwords, so
# point them at a fast hasher.  (Other apps' fixtures skip passwords entirely.)
FAST_HASHER = override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)


class UserModelTests(TestCase):
    """The accounts.User model is wired up as the project's user model."""

    def test_get_user_model_is_accounts_user(self) -> None:
        self.assertEqual(User._meta.label, "accounts.User")

    def test_default_role_is_learner(self) -> None:
        user = User.objects.create_user(username="pat")
        self.assertEqual(user.role, User.Role.LEARNER)

    def test_is_learner_true_for_default_role(self) -> None:
        user = User.objects.create_user(username="pat")
        self.assertTrue(user.is_learner)
        self.assertFalse(user.is_instructor)

    def test_is_instructor_true_for_instructor_role(self) -> None:
        user = User.objects.create_user(username="sam", role=User.Role.INSTRUCTOR)
        self.assertTrue(user.is_instructor)
        self.assertFalse(user.is_learner)

    def test_str_returns_username(self) -> None:
        user = User.objects.create_user(username="pat")
        self.assertEqual(str(user), "pat")


class HomeRedirectTests(TestCase):
    """'/' routes each visitor to the landing page for their role."""

    def test_anonymous_visitor_goes_to_login(self) -> None:
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("accounts:login"))

    def test_learner_goes_to_catalogue(self) -> None:
        user = User.objects.create_user(username="learner")
        self.client.force_login(user)
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("courses:catalogue"))

    def test_instructor_goes_to_dashboard(self) -> None:
        user = User.objects.create_user(username="teacher", role=User.Role.INSTRUCTOR)
        self.client.force_login(user)
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("accounts:dashboard"))


class DashboardAccessTests(TestCase):
    """``/accounts/dashboard/`` is an instructor page, so it needs a role gate.

    Regression test for H-1's sibling finding (M-1): the view used to be a
    plain ``TemplateView`` that rendered for anonymous visitors.
    """

    def test_anonymous_is_redirected_to_login(self) -> None:
        url = reverse("accounts:dashboard")
        self.assertRedirects(
            self.client.get(url), f"{reverse('accounts:login')}?next={url}"
        )

    def test_learner_is_forbidden(self) -> None:
        self.client.force_login(User.objects.create_user(username="learner"))
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertEqual(response.status_code, FORBIDDEN)

    def test_instructor_sees_the_dashboard(self) -> None:
        user = User.objects.create_user(username="teacher", role=User.Role.INSTRUCTOR)
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("courses:instructor_course_list"))


class BaseTemplateTests(TestCase):
    """base.html renders a role-aware nav; error pages use it too.

    Nav assertions are made against a 404 page, which renders base.html and
    needs no course data to exist yet.
    """

    def _get_404_page(self) -> HttpResponse:
        response = self.client.get("/no-such-page/")
        self.assertEqual(response.status_code, NOT_FOUND)
        return response

    def test_unknown_url_renders_404_template(self) -> None:
        response = self._get_404_page()
        self.assertContains(response, "Page not found", status_code=NOT_FOUND)
        self.assertContains(response, "Skip to main content", status_code=NOT_FOUND)

    def test_anonymous_nav_offers_login_and_signup(self) -> None:
        response = self._get_404_page()
        self.assertContains(response, reverse("accounts:login"), status_code=NOT_FOUND)
        self.assertContains(response, reverse("accounts:signup"), status_code=NOT_FOUND)
        self.assertNotContains(
            response, reverse("courses:catalogue"), status_code=NOT_FOUND
        )
        self.assertNotContains(
            response, reverse("accounts:dashboard"), status_code=NOT_FOUND
        )

    def test_learner_nav_links_to_catalogue_and_logout(self) -> None:
        user = User.objects.create_user(username="learner")
        self.client.force_login(user)
        response = self._get_404_page()
        self.assertContains(
            response, reverse("courses:catalogue"), status_code=NOT_FOUND
        )
        self.assertContains(response, reverse("accounts:logout"), status_code=NOT_FOUND)
        self.assertNotContains(
            response, reverse("accounts:dashboard"), status_code=NOT_FOUND
        )

    def test_instructor_nav_links_to_dashboard(self) -> None:
        user = User.objects.create_user(username="teacher", role=User.Role.INSTRUCTOR)
        self.client.force_login(user)
        response = self._get_404_page()
        self.assertContains(
            response, reverse("accounts:dashboard"), status_code=NOT_FOUND
        )
        self.assertContains(response, reverse("accounts:logout"), status_code=NOT_FOUND)
        self.assertNotContains(response, 'href="/courses/"', status_code=NOT_FOUND)

    def test_logout_link_is_post_form_with_csrf(self) -> None:
        user = User.objects.create_user(username="learner")
        self.client.force_login(user)
        response = self._get_404_page()
        self.assertContains(
            response, 'method="post"', status_code=NOT_FOUND, html=False
        )
        self.assertContains(
            response, "csrfmiddlewaretoken", status_code=NOT_FOUND, html=False
        )

    def test_403_and_500_templates_render(self) -> None:
        for template_name in ("403.html", "500.html"):
            html = render_to_string(template_name)
            self.assertIn("Skip to main content", html)


@FAST_HASHER
class SignUpTests(TestCase):
    """Signup creates the chosen account but does not log the user in."""

    def setUp(self) -> None:
        self.url = reverse("accounts:signup")

    def _payload(self, **overrides: str) -> dict[str, str]:
        data = {
            "username": "newbie",
            "email": "newbie@example.com",
            "role": User.Role.LEARNER,
            "password1": "complex-passphrase-9",
            "password2": "complex-passphrase-9",
        }
        data.update(overrides)
        return data

    def test_get_renders_form_with_labelled_fields_and_help_text(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'for="id_username"', html=False)
        self.assertContains(response, "Password confirmation")  # label_tag output
        # The role field is an opt-in: it is not offered by default (H-1).
        self.assertNotContains(response, 'name="role"', html=False)

    @override_settings(ALLOW_INSTRUCTOR_SELF_SIGNUP=True)
    def test_role_field_is_offered_when_self_signup_is_enabled(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="role"', html=False)

    @override_settings(ALLOW_INSTRUCTOR_SELF_SIGNUP=True)
    def test_instructor_role_is_accepted(self) -> None:
        # The demo opt-in still works: with the setting on, a visitor may
        # choose the instructor role at signup.
        response = self.client.post(self.url, self._payload(role=User.Role.INSTRUCTOR))
        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(username="newbie")
        self.assertEqual(user.role, User.Role.INSTRUCTOR)
        self.assertEqual(user.email, "newbie@example.com")

    def test_instructor_self_signup_is_disabled_by_default(self) -> None:
        self.assertFalse(settings.ALLOW_INSTRUCTOR_SELF_SIGNUP)

    def test_forged_instructor_role_is_forced_to_learner_by_default(self) -> None:
        # Regression (H-1): with the default flipped to False, a crafted POST
        # asking for the instructor role must still produce a learner.
        response = self.client.post(self.url, self._payload(role=User.Role.INSTRUCTOR))
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertEqual(User.objects.get(username="newbie").role, User.Role.LEARNER)

    def test_success_message_and_no_auto_login(self) -> None:
        response = self.client.post(self.url, self._payload(), follow=True)
        self.assertContains(response, "Your account has been created")
        # Not authenticated: signup must send the user through the login form.
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_duplicate_username_is_rejected(self) -> None:
        User.objects.create_user(username="newbie")
        response = self.client.post(self.url, self._payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username="newbie").count(), 1)

    def test_password_mismatch_is_rejected(self) -> None:
        response = self.client.post(
            self.url, self._payload(password2="different-pass-1")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newbie").exists())

    @override_settings(ALLOW_INSTRUCTOR_SELF_SIGNUP=False)
    def test_role_field_hidden_when_self_signup_disabled(self) -> None:
        response = self.client.get(self.url)
        self.assertNotContains(response, 'name="role"', html=False)

    @override_settings(ALLOW_INSTRUCTOR_SELF_SIGNUP=False)
    def test_forged_instructor_role_is_forced_to_learner(self) -> None:
        # Even though the field is hidden, a crafted POST asks for instructor.
        response = self.client.post(self.url, self._payload(role=User.Role.INSTRUCTOR))
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertEqual(User.objects.get(username="newbie").role, User.Role.LEARNER)


@FAST_HASHER
class LoginLogoutTests(TestCase):
    """Login/logout use Django's built-in views with our template and messages."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="pat", password="complex-passphrase-9"
        )
        self.login_url = reverse("accounts:login")

    def test_get_renders_form_with_next_hidden_field(self) -> None:
        response = self.client.get(self.login_url, {"next": "/somewhere/"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="next"', html=False)
        self.assertContains(response, 'value="/somewhere/"', html=False)

    def test_valid_login_redirects_to_home_by_default(self) -> None:
        response = self.client.post(
            self.login_url,
            {"username": "pat", "password": "complex-passphrase-9"},
        )
        self.assertEqual(response.url, reverse("home"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_safe_next_is_honoured(self) -> None:
        target = reverse("courses:catalogue")
        response = self.client.post(
            self.login_url,
            {"username": "pat", "password": "complex-passphrase-9", "next": target},
        )
        self.assertEqual(response.url, target)

    def test_external_next_is_rejected(self) -> None:
        response = self.client.post(
            self.login_url,
            {
                "username": "pat",
                "password": "complex-passphrase-9",
                "next": "https://evil.example.com/",
            },
        )
        self.assertEqual(response.url, reverse("home"))

    def test_login_success_message(self) -> None:
        response = self.client.post(
            self.login_url,
            {"username": "pat", "password": "complex-passphrase-9"},
            follow=True,
        )
        self.assertContains(response, "Welcome back, pat.")

    def test_invalid_login_stays_on_page(self) -> None:
        response = self.client.post(
            self.login_url, {"username": "pat", "password": "wrong-password"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_rejects_get(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 405)

    def test_logout_post_logs_out_and_redirects_to_login(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)
