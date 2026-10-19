"""Forms for the courses app: authoring plus the catalogue filter.

The authoring forms deliberately omit ``status`` (courses move draft/published
only through the publish toggle) and ``instructor`` (set from the logged-in
user in the view, never from the submitted data).
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from accounts.models import User

from .models import Course, Lesson
from .validators import validate_video_url

# Catalogue sort keys; the view switches on the cleaned value.
SORT_NEWEST = "newest"
SORT_TITLE = "title"
SORT_CHOICES = [
    (SORT_NEWEST, _("Newest first")),
    (SORT_TITLE, _("Title (A–Z)")),
]


class CatalogueFilterForm(forms.Form):
    """Validate every GET parameter the catalogue accepts.

    The catalogue is a read-only listing, so this is a plain ``Form`` (not a
    ``ModelForm``) bound to ``request.GET``.  Every field is optional: an
    absent or invalid value is simply dropped, and the view falls back to the
    default, so a hand-edited URL can never raise a 500.
    """

    q = forms.CharField(
        label=_("Search"),
        required=False,
        max_length=100,
        help_text=_("Matches the course title or description."),
    )
    instructor = forms.ModelChoiceField(
        label=_("Instructor"),
        required=False,
        empty_label=_("All instructors"),
        queryset=User.objects.filter(role=User.Role.INSTRUCTOR).order_by("username"),
    )
    sort = forms.ChoiceField(
        label=_("Sort by"),
        required=False,
        choices=SORT_CHOICES,
        initial=SORT_NEWEST,
    )
    # Validated here as well so pagination can trust it; kept out of the
    # rendered filter bar (it is carried by the page links instead).
    page = forms.IntegerField(required=False, min_value=1)


class CourseForm(forms.ModelForm):
    """Create or edit a course the instructor owns."""

    enforce_order = forms.BooleanField(
        label=_("Enforce order"),
        required=False,
        help_text=_("Learners must complete the lessons in order."),
    )

    class Meta:
        model = Course
        fields = ["title", "description", "enforce_order"]
        widgets = {"description": forms.Textarea(attrs={"rows": 6})}


class LessonForm(forms.ModelForm):
    """Create or edit a lesson; position is assigned by the model."""

    # Redeclared so only http/https links pass (the model's URLField would
    # also accept ftp/ftps).
    video_url = forms.URLField(
        label=_("Video URL"),
        required=False,
        validators=[validate_video_url],
    )

    class Meta:
        model = Lesson
        fields = ["title", "content", "video_url", "attachment"]
        widgets = {"content": forms.Textarea(attrs={"rows": 10})}
