"""Forms for the accounts app."""

from django.conf import settings
from django.contrib.auth.forms import UserCreationForm

from .models import User


class SignUpForm(UserCreationForm):
    """Create an account: username, email, password1/password2 and role.

    ``UserCreationForm`` supplies password1/password2 with Django's password
    validators and help text; the extra model fields come from ``fields``.

    Instructor self-signup is OFF by default (``ALLOW_INSTRUCTOR_SELF_SIGNUP``):
    the instructor role grants course-authoring rights, so in production an
    administrator grants it through Django admin instead.  Switching the setting
    on adds the role field back for a demo environment, and a crafted POST is
    still forced to the learner role while it is off.
    """

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ["username", "email", "role"]

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        if not settings.ALLOW_INSTRUCTOR_SELF_SIGNUP:
            # Hide the field entirely...
            self.fields.pop("role", None)

    def save(self, commit: bool = True) -> User:
        # ...and force the value on save, so a forged POST cannot smuggle the
        # instructor role in while the field is hidden.
        if not settings.ALLOW_INSTRUCTOR_SELF_SIGNUP:
            self.instance.role = User.Role.LEARNER
        return super().save(commit=commit)
