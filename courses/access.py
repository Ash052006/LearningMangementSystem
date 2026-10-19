"""Who may see which course.

The learner-visibility rule lives here once, so the web views and the JSON API
cannot drift apart.  A course is visible to a user when it is published, when
the user owns it (the instructor previewing their own draft), or when the user
is enrolled -- so a learner who started a course keeps access to their history
after it is withdrawn.  Everything else is hidden as a 404, the same answer the
catalogue already gives for a course the user may not see.
"""

from django.http import Http404

from .models import Course, Enrollment

HIDDEN_COURSE_MESSAGE = "This course is not available."


def ensure_course_visible(user: object, course: Course) -> Course:
    """Return ``course`` when ``user`` may see it, otherwise raise ``Http404``."""
    if course.is_published:
        return course
    if course.instructor_id == getattr(user, "pk", None):
        return course
    if Enrollment.objects.filter(user=user, course=course).exists():
        return course
    raise Http404(HIDDEN_COURSE_MESSAGE)
