"""Views for the courses app.

Instructor-facing course and lesson authoring lives here, alongside the
learner-facing catalogue, course overview and lesson flow.  Access control is
delegated to the mixins in ``accounts.mixins``: ``InstructorRequiredMixin``
rejects non-instructors (403) and ``OwnerCourseMixin`` scopes course lookups
to the logged-in instructor (404 for anyone else's course).
"""

import csv
import os
import re
from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, QuerySet
from django.db.models.deletion import ProtectedError
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from accounts.mixins import InstructorRequiredMixin, OwnerCourseMixin
from quizzes.models import Quiz

from .access import ensure_course_visible
from .forms import (
    SORT_NEWEST,
    SORT_TITLE,
    CatalogueFilterForm,
    CourseForm,
    LessonForm,
)
from .models import Certificate, Course, Enrollment, Lesson
from .services import (
    CourseNotPublishableError,
    can_access_lesson,
    certificate_eligible,
    course_learner_stats,
    course_progress,
    course_question_stats,
    enrol,
    enrollments_with_progress,
    first_blocking_lesson,
    issue_certificate_if_eligible,
    mark_lesson_complete,
    move_lesson_down,
    move_lesson_up,
    progress_percent,
    published_courses,
    resequence_lessons,
    toggle_course_publish,
    unlocked_lesson_ids,
)
from .validators import attachment_content_type


class CatalogueView(LoginRequiredMixin, ListView):
    """Published courses, with search, an instructor filter, sorting and pages.

    ``lesson_count`` is annotated in the database so listing a page of courses
    never runs a count query per row (no N+1).  Every GET parameter is read
    through ``CatalogueFilterForm``; a value that does not validate is dropped
    and the default is used instead.

    The base queryset is always filtered to ``PUBLISHED``, so no combination of
    search or filter parameters can ever surface a draft.
    """

    template_name = "courses/catalogue.html"
    context_object_name = "courses"
    paginate_by = 9

    def _filters(self) -> dict[str, Any]:
        """Return the cleaned GET parameters (valid fields only).

        An unbound form (no query string) yields ``{}``.  A bound form with an
        invalid field still keeps the fields that did validate, so one bad
        parameter does not discard the rest.
        """
        form = self.get_filter_form()
        form.is_valid()
        return getattr(form, "cleaned_data", {})

    def get_filter_form(self) -> CatalogueFilterForm:
        """Build (once) the filter form bound to the request's GET data."""
        if not hasattr(self, "_filter_form"):
            self._filter_form = CatalogueFilterForm(self.request.GET or None)
        return self._filter_form

    def get_queryset(self) -> QuerySet[Course]:
        filters = self._filters()
        queryset = published_courses()
        search = filters.get("q")
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) | Q(description__icontains=search)
            )
        instructor = filters.get("instructor")
        if instructor is not None:
            queryset = queryset.filter(instructor=instructor)
        queryset = queryset.select_related("instructor").annotate(
            lesson_count=Count("lessons")
        )
        if filters.get("sort") == SORT_TITLE:
            return queryset.order_by("title", "pk")
        return queryset.order_by("-created_at", "-pk")

    def paginate_queryset(
        self, queryset: QuerySet[Course], page_size: int
    ) -> tuple[Any, Any, list[Course], bool]:
        """Paginate using the validated ``page`` parameter.

        ``Paginator.get_page`` clamps an out-of-range number to the first or
        last page, so a link to ``?page=9999`` shows a page rather than a 404.
        """
        paginator = self.get_paginator(queryset, page_size)
        page = paginator.get_page(self._filters().get("page") or 1)
        return (paginator, page, page.object_list, page.has_other_pages())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.get_filter_form()
        # Pagination links carry the current filters but not the page number.
        context["querystring"] = self._querystring()
        return context

    def _querystring(self) -> str:
        """Re-encode the validated filters for the page links."""
        filters = self._filters()
        params: dict[str, str] = {}
        if filters.get("q"):
            params["q"] = filters["q"]
        if filters.get("instructor") is not None:
            params["instructor"] = str(filters["instructor"].pk)
        if filters.get("sort") and filters["sort"] != SORT_NEWEST:
            params["sort"] = filters["sort"]
        return urlencode(params)


class InstructorCourseListView(InstructorRequiredMixin, ListView):
    """The logged-in instructor's own courses, newest first."""

    template_name = "courses/instructor/course_list.html"
    context_object_name = "courses"

    def get_queryset(self) -> QuerySet[Course]:
        return Course.objects.filter(instructor=self.request.user).annotate(
            lesson_count=Count("lessons")
        )


class InstructorDashboardView(InstructorRequiredMixin, ListView):
    """Index of the instructor's own courses, with learner counts.

    Both counts are annotated in the database (``distinct=True`` so the lesson
    and enrolment joins do not multiply each other), keeping the page a
    constant number of queries regardless of how many courses exist.
    """

    template_name = "courses/instructor/dashboard.html"
    context_object_name = "courses"

    def get_queryset(self) -> QuerySet[Course]:
        return (
            Course.objects.filter(instructor=self.request.user)
            .annotate(
                lesson_count=Count("lessons", distinct=True),
                learner_count=Count("enrollments", distinct=True),
            )
            .order_by("-created_at")
        )


class CourseDashboardView(OwnerCourseMixin, DetailView):
    """Per-course progress: learner table, summary totals and analytics."""

    template_name = "courses/instructor/course_dashboard.html"
    context_object_name = "course"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        rows = course_learner_stats(self.object)
        context["learner_rows"] = rows
        context["enrolled_count"] = len(rows)
        context["completed_count"] = sum(
            1
            for row in rows
            if row.total_lessons and row.lessons_completed == row.total_lessons
        )
        context["passed_count"] = sum(1 for row in rows if row.passed)
        context["question_rows"] = course_question_stats(self.object)
        return context


# Leading whitespace and control characters a spreadsheet strips before it
# decides whether a cell is a formula (spaces, tabs, newlines and the like).
_CSV_LEADING_NOISE = re.compile(r"^[\s\x00-\x1f\x7f-\x9f\u200b-\u200f\ufeff]*")


def _csv_safe(value: object) -> str:
    """Format one CSV cell, neutralising spreadsheet formula injection.

    Excel and Sheets treat a cell beginning with ``=``, ``+``, ``-`` or ``@``
    as a formula, so a username like ``=SUM(A1)`` could run when the export is
    opened.  Prefixing a single quote forces the cell to be read as text.

    The dangerous character is looked for *after* any leading whitespace or
    control characters, because a spreadsheet ignores those too: ``"\\t=cmd"``
    is just as executable as ``"=cmd"``.  Numeric values are passed through
    untouched (they never start with one of those characters); only genuine
    text cells are given the quote.
    """
    text = "" if value is None else str(value)
    if _CSV_LEADING_NOISE.sub("", text)[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


class CourseDashboardCsvView(OwnerCourseMixin, View):
    """Download the per-course learner table as CSV (same rows as the page)."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = get_object_or_404(self.get_queryset(), pk=kwargs["pk"])
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="course-{course.pk}-learners.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                _("Username"),
                _("Enrolled at"),
                _("Lessons completed"),
                _("Total lessons"),
                _("Progress (%)"),
                _("Best quiz (%)"),
                _("Passed"),
                _("Attempts"),
            ]
        )
        for row in course_learner_stats(course):
            best = (
                "" if row.best_quiz_percent is None else f"{row.best_quiz_percent:.2f}"
            )
            writer.writerow(
                [
                    _csv_safe(row.username),
                    _csv_safe(row.enrolled_at.strftime("%Y-%m-%d %H:%M")),
                    row.lessons_completed,
                    row.total_lessons,
                    row.percent,
                    best,
                    _("Yes") if row.passed else _("No"),
                    row.attempts_count,
                ]
            )
        return response


class CourseCreateView(InstructorRequiredMixin, CreateView):
    """Create a course owned by the logged-in instructor."""

    form_class = CourseForm
    template_name = "courses/instructor/course_form.html"
    success_url = reverse_lazy("courses:instructor_course_list")

    def form_valid(self, form: CourseForm) -> HttpResponse:
        # The instructor comes from the session, never from the posted form.
        form.instance.instructor = self.request.user
        response = super().form_valid(form)
        messages.success(
            self.request,
            _('Course "%(title)s" created.') % {"title": self.object.title},
        )
        return response


class CourseDetailView(OwnerCourseMixin, DetailView):
    """An instructor's management page for one of their courses."""

    template_name = "courses/instructor/course_detail.html"
    context_object_name = "course"


class CourseUpdateView(OwnerCourseMixin, UpdateView):
    """Edit a course the instructor owns."""

    form_class = CourseForm
    template_name = "courses/instructor/course_form.html"

    def get_success_url(self) -> str:
        return reverse("courses:course_detail", args=[self.object.pk])

    def form_valid(self, form: CourseForm) -> HttpResponse:
        response = super().form_valid(form)
        messages.success(
            self.request,
            _('Course "%(title)s" updated.') % {"title": self.object.title},
        )
        return response


class CourseDeleteView(OwnerCourseMixin, DeleteView):
    """Delete a course the instructor owns (GET confirms, POST deletes)."""

    template_name = "courses/instructor/course_confirm_delete.html"
    success_url = reverse_lazy("courses:instructor_course_list")

    def form_valid(self, form: Any) -> HttpResponse:
        course = self.get_object()
        try:
            course.delete()
        except ProtectedError:
            # AttemptAnswer -> Question is PROTECT: a course whose quiz has
            # answered questions cannot be removed without losing that record.
            messages.error(
                self.request,
                _(
                    "This course cannot be deleted because its quiz has already "
                    "been answered by a learner."
                ),
            )
            return redirect("courses:course_detail", pk=course.pk)
        messages.success(
            self.request, _('Course "%(title)s" deleted.') % {"title": course.title}
        )
        return HttpResponseRedirect(self.get_success_url())


class CourseTogglePublishView(OwnerCourseMixin, View):
    """POST-only: publish an owned course (needs a lesson) or unpublish it."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = get_object_or_404(self.get_queryset(), pk=kwargs["pk"])
        try:
            published = toggle_course_publish(course)
        except CourseNotPublishableError:
            messages.error(
                request,
                _("Add at least one lesson before publishing this course."),
            )
        else:
            if published:
                messages.success(
                    request,
                    _('Course "%(title)s" published.') % {"title": course.title},
                )
                # A quiz with no questions is not shown to learners, even
                # though the course itself is now published.
                if Quiz.objects.filter(course=course, questions__isnull=True).exists():
                    messages.warning(
                        request,
                        _(
                            "The quiz will not be shown to learners until it has "
                            "at least one question."
                        ),
                    )
            else:
                messages.success(
                    request,
                    _('Course "%(title)s" unpublished.') % {"title": course.title},
                )
        return redirect("courses:course_detail", pk=course.pk)


class NestedLessonMixin(InstructorRequiredMixin):
    """Resolve the nested course and lesson while enforcing ownership.

    Lesson URLs are nested under a course (``course_pk``); both lookups are
    scoped to the request user, so another instructor gets a 404.
    """

    def get_course(self) -> Course:
        return get_object_or_404(
            Course, pk=self.kwargs["course_pk"], instructor=self.request.user
        )

    def get_lesson(self) -> Lesson:
        return get_object_or_404(self.get_course().lessons, pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["course"] = self.get_course()
        return context

    def course_success_url(self) -> str:
        return reverse("courses:course_detail", args=[self.kwargs["course_pk"]])


class LessonCreateView(NestedLessonMixin, CreateView):
    """Add a lesson to a course the instructor owns."""

    form_class = LessonForm
    template_name = "courses/instructor/lesson_form.html"

    def form_valid(self, form: LessonForm) -> HttpResponse:
        form.instance.course = self.get_course()
        response = super().form_valid(form)
        messages.success(
            self.request,
            _('Lesson "%(title)s" added.') % {"title": self.object.title},
        )
        return response

    def get_success_url(self) -> str:
        return self.course_success_url()


class LessonUpdateView(NestedLessonMixin, UpdateView):
    """Edit a lesson of a course the instructor owns."""

    form_class = LessonForm
    template_name = "courses/instructor/lesson_form.html"

    def get_object(self, queryset: Any = None) -> Lesson:
        return self.get_lesson()

    def form_valid(self, form: LessonForm) -> HttpResponse:
        response = super().form_valid(form)
        messages.success(
            self.request,
            _('Lesson "%(title)s" updated.') % {"title": self.object.title},
        )
        return response

    def get_success_url(self) -> str:
        return self.course_success_url()


class LessonDeleteView(NestedLessonMixin, DeleteView):
    """Delete a lesson, then close the gap in the remaining positions."""

    template_name = "courses/instructor/lesson_confirm_delete.html"

    def get_object(self, queryset: Any = None) -> Lesson:
        return self.get_lesson()

    def form_valid(self, form: Any) -> HttpResponse:
        course = self.get_course()
        lesson = self.get_object()
        lesson.delete()
        resequence_lessons(course)
        messages.success(
            self.request, _('Lesson "%(title)s" deleted.') % {"title": lesson.title}
        )
        return redirect("courses:course_detail", pk=course.pk)


class LessonMoveView(NestedLessonMixin, View):
    """POST-only base view: move a lesson one place in its course."""

    direction = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        lesson = self.get_lesson()
        mover = move_lesson_up if self.direction == "up" else move_lesson_down
        if mover(lesson):
            messages.success(
                request,
                _('Lesson "%(title)s" moved %(direction)s.')
                % {"title": lesson.title, "direction": self.direction},
            )
        else:
            messages.info(
                request,
                _('Lesson "%(title)s" is already at the %(edge)s.')
                % {
                    "title": lesson.title,
                    "edge": _("top") if self.direction == "up" else _("bottom"),
                },
            )
        return redirect("courses:course_detail", pk=self.kwargs["course_pk"])


class LessonMoveUpView(LessonMoveView):
    """POST-only: swap a lesson with the one above it."""

    direction = "up"


class LessonMoveDownView(LessonMoveView):
    """POST-only: swap a lesson with the one below it."""

    direction = "down"


class LearnerCourseAccessMixin(LoginRequiredMixin):
    """Resolve a course for a learner, applying the unpublish policy.

    A course is available when it is published, or when the learner enrolled
    while it was published and it has since been withdrawn: unpublishing hides
    a course from the catalogue and blocks *new* enrolments, but anyone who
    already started keeps access to their history.  Everything else is a 404,
    the same response the catalogue already implies for hidden courses.
    """

    def get_course(self) -> Course:
        course = get_object_or_404(Course, pk=self.kwargs["pk"])
        # Shared with the API so both apply the exact same visibility rule.
        return ensure_course_visible(self.request.user, course)

    def get_enrollment(self, course: Course) -> Enrollment:
        return get_object_or_404(Enrollment, user=self.request.user, course=course)

    def is_preview(self, course: Course) -> bool:
        """True when the owning instructor is previewing their own course."""
        return course.instructor_id == self.request.user.pk


class EnrolView(LoginRequiredMixin, View):
    """POST-only: enrol the learner in a published course (idempotent)."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        # Only published courses accept new enrolments.
        course = get_object_or_404(
            Course, pk=kwargs["pk"], status=Course.Status.PUBLISHED
        )
        enrol(request.user, course)
        messages.success(
            request, _('You are enrolled in "%(title)s".') % {"title": course.title}
        )
        return redirect("courses:learner_course_detail", pk=course.pk)


class LearnerCourseDetailView(LearnerCourseAccessMixin, DetailView):
    """Learner overview of a course: lessons plus enrol or progress."""

    template_name = "courses/course_detail.html"
    context_object_name = "course"

    def get_object(self, queryset: Any = None) -> Course:
        return self.get_course()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        course = self.object
        lessons = list(course.lessons.all())
        enrollment = Enrollment.objects.filter(
            user=self.request.user, course=course
        ).first()
        completed_ids: set[int] = set()
        unlocked_ids: set[int] = set()
        if enrollment is not None:
            completed_ids = set(
                enrollment.lesson_progress.values_list("lesson_id", flat=True)
            )
            unlocked_ids = unlocked_lesson_ids(enrollment, completed_ids, lessons)
        preview = self.is_preview(course)
        if preview:
            # A previewing instructor is exempt from ordering: everything opens.
            unlocked_ids = {lesson.pk for lesson in lessons}
        # With ordering on, every locked lesson is gated by the same lesson: the
        # first one the learner has not finished.  Naming it explains the lock.
        blocking = next(
            (lesson for lesson in lessons if lesson.pk not in completed_ids), None
        )
        context["lessons"] = lessons
        context["enrollment"] = enrollment
        context["quiz"] = None
        context["is_preview"] = preview
        # A certificate is shown once the learner is eligible, and stays shown
        # afterwards even if the course changes (an already-issued certificate
        # is never revoked), so the button keys off both facts.
        context["certificate"] = None
        context["certificate_eligible"] = False
        context["lesson_rows"] = [
            {
                "lesson": lesson,
                "completed": lesson.pk in completed_ids,
                "unlocked": lesson.pk in unlocked_ids,
                "locked_by": blocking if lesson.pk not in unlocked_ids else None,
            }
            for lesson in lessons
        ]
        if enrollment is not None:
            context["progress"] = course_progress(enrollment)
            # Continue at the first unfinished lesson, or the first lesson when
            # everything is done (so the learner can revise).
            context["continue_lesson"] = next(
                (lesson for lesson in lessons if lesson.pk not in completed_ids),
                lessons[0] if lessons else None,
            )
            # Only an enrolled learner can take the quiz, and only once it has
            # questions (an empty quiz is invisible to learners).
            try:
                quiz = course.quiz
            except Quiz.DoesNotExist:
                quiz = None
            context["quiz"] = quiz if quiz is not None and quiz.has_questions else None
            certificate = Certificate.objects.filter(enrollment=enrollment).first()
            context["certificate"] = certificate
            context["certificate_eligible"] = (
                certificate is not None or certificate_eligible(enrollment)
            )
        return context


class CertificateIssueView(LearnerCourseAccessMixin, View):
    """POST-only: issue the certificate when the learner has earned it.

    Issuing is a write, so it follows the project's POST-only rule (the course
    page's "Download certificate" button is a form).  The learner is then
    redirected to the printable page, so refreshing or printing cannot re-post.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        if self.is_preview(course):
            # An instructor previewing their own course has no enrolment and
            # does not earn a certificate for it.
            raise Http404("Instructors do not earn certificates for their own courses.")
        enrollment = self.get_enrollment(course)
        # An already-issued certificate is reused regardless of the current
        # requirements; otherwise try to issue one now.
        certificate = Certificate.objects.filter(enrollment=enrollment).first()
        if certificate is None:
            certificate = issue_certificate_if_eligible(enrollment)
        if certificate is None:
            messages.warning(
                request,
                _("Finish every lesson and pass the quiz to earn your certificate."),
            )
            return redirect("courses:learner_course_detail", pk=course.pk)
        messages.success(request, _("Your certificate is ready."))
        return redirect("courses:certificate", pk=course.pk)


class CertificateView(LearnerCourseAccessMixin, View):
    """GET: the printable certificate for an enrolled learner.

    Read-only: the certificate is issued by ``CertificateIssueView`` (a POST),
    so this page never writes.  It only shows the certificate belonging to the
    requester's own enrolment.
    """

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        if self.is_preview(course):
            raise Http404("Instructors do not earn certificates for their own courses.")
        enrollment = self.get_enrollment(course)
        certificate = get_object_or_404(Certificate, enrollment=enrollment)
        return render(
            request,
            "courses/certificate.html",
            {"certificate": certificate, "course": course},
        )


class CertificateVerifyView(View):
    """Public verification page: ``/certificates/<uuid>/``.

    Anyone holding the random code can confirm it is genuine.  Only the facts
    already printed on the certificate are shown (holder name, course, issue
    date, code); the enrolment, the account (email, username) and any progress
    are never exposed.  An unknown code reveals nothing beyond "not found".
    No login is required, which is the point of a verification page.
    """

    template_name = "courses/verify_certificate.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        certificate = (
            Certificate.objects.select_related("enrollment__user", "enrollment__course")
            .filter(code=kwargs["code"])
            .first()
        )
        context = {"certificate": certificate}
        # A missing code is a 404, but the template still renders a clear
        # "not found" message rather than Django's default error page.
        status = 200 if certificate is not None else 404
        return render(request, self.template_name, context, status=status)


def locked_lesson_message(enrollment: Enrollment, lesson: Lesson) -> str:
    """The learner-facing reason ``lesson`` is still locked for ``enrollment``.

    Shared by the lesson page, the mark-complete POST and the attachment
    download, so all three explain the same block in exactly the same words.
    """
    blocking = first_blocking_lesson(enrollment, lesson)
    if blocking is None:
        return _("This lesson is locked.")
    return _('Complete "%(title)s" first, then this lesson will unlock.') % {
        "title": blocking.title
    }


class LearnerLessonView(LearnerCourseAccessMixin, DetailView):
    """One lesson for an enrolled learner, with next/previous navigation."""

    template_name = "courses/lesson.html"
    context_object_name = "lesson"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.course = self.get_course()
        self.preview = self.is_preview(self.course)
        if not self.preview:
            if not Enrollment.objects.filter(
                user=request.user, course=self.course
            ).exists():
                messages.info(request, _("Enrol in this course to open its lessons."))
                return redirect("courses:learner_course_detail", pk=self.course.pk)
            # Ordering is enforced here, not just by hiding outline links: a
            # learner who types a later lesson's URL is sent back to the course.
            enrollment = self.get_enrollment(self.course)
            if not can_access_lesson(enrollment, self.get_object()):
                messages.warning(request, self._locked_message(enrollment))
                return redirect("courses:learner_course_detail", pk=self.course.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset: Any = None) -> Lesson:
        if not hasattr(self, "_lesson"):
            self._lesson = get_object_or_404(
                self.course.lessons, pk=self.kwargs["lesson_pk"]
            )
        return self._lesson

    def _locked_message(self, enrollment: Enrollment) -> str:
        return locked_lesson_message(enrollment, self.get_object())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        lessons = list(self.course.lessons.all())
        index = lessons.index(self.object)
        next_lesson = lessons[index + 1] if index + 1 < len(lessons) else None
        context["course"] = self.course
        context["is_preview"] = self.preview
        context["previous_lesson"] = lessons[index - 1] if index > 0 else None
        context["next_lesson"] = next_lesson
        if self.preview:
            context["enrollment"] = None
            context["progress"] = None
            context["is_complete"] = False
            context["next_unlocked"] = True
            return context
        enrollment = self.get_enrollment(self.course)
        unlocked_ids = unlocked_lesson_ids(enrollment)
        context["enrollment"] = enrollment
        context["progress"] = course_progress(enrollment)
        context["is_complete"] = enrollment.lesson_progress.filter(
            lesson=self.object
        ).exists()
        context["next_unlocked"] = next_lesson is None or next_lesson.pk in unlocked_ids
        return context


class LessonCompleteView(LearnerCourseAccessMixin, View):
    """POST-only: mark one lesson complete for the enrolled learner."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course = self.get_course()
        enrollment = get_object_or_404(Enrollment, user=request.user, course=course)
        lesson = get_object_or_404(course.lessons, pk=kwargs["lesson_pk"])
        # The same gate as the lesson view, re-checked here so a learner cannot
        # skip ahead by POSTing directly to a later lesson's complete URL.  The
        # owning instructor previewing is exempt.
        if not self.is_preview(course) and not can_access_lesson(enrollment, lesson):
            messages.warning(request, locked_lesson_message(enrollment, lesson))
            return redirect("courses:learner_course_detail", pk=course.pk)
        created = mark_lesson_complete(enrollment, lesson)
        if created:
            messages.success(
                request,
                _('Lesson "%(title)s" marked complete.') % {"title": lesson.title},
            )
        else:
            messages.info(request, _("You have already completed this lesson."))
        lessons = list(course.lessons.all())
        index = lessons.index(lesson)
        if index + 1 < len(lessons):
            return redirect(
                "courses:learner_lesson_detail",
                pk=course.pk,
                lesson_pk=lessons[index + 1].pk,
            )
        return redirect("courses:learner_course_detail", pk=course.pk)


class LessonAttachmentView(LoginRequiredMixin, View):
    """Stream a lesson attachment to its course owner or an enrolled learner.

    Attachments are private: they are never served from the media URL.  The
    file is read through the storage backend and returned as a download only
    after the access check below, with a content type taken from the allowlist
    (never guessed from the file) and a ``Content-Disposition: attachment``
    header that Django builds safely from the filename.

    Lesson ordering applies here too: an enrolled learner who has not reached a
    lesson cannot download its file from the direct URL, exactly as they cannot
    open the lesson page or mark it complete.
    """

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        course, enrollment = self._get_access(request, kwargs["pk"])
        lesson = get_object_or_404(course.lessons, pk=kwargs["lesson_pk"])
        # The same gate as the lesson page and the mark-complete POST.  The
        # owning instructor (``enrollment is None``) previews freely.
        if enrollment is not None and not can_access_lesson(enrollment, lesson):
            messages.warning(request, locked_lesson_message(enrollment, lesson))
            return redirect("courses:learner_course_detail", pk=course.pk)
        if not lesson.attachment:
            raise Http404("This lesson has no attachment.")
        content_type = attachment_content_type(lesson.attachment.name)
        if content_type is None:
            raise Http404("This attachment type cannot be downloaded.")
        try:
            file = lesson.attachment.open("rb")
        except FileNotFoundError:
            raise Http404("The attachment file is missing.") from None
        return FileResponse(
            file,
            as_attachment=True,
            filename=os.path.basename(lesson.attachment.name),
            content_type=content_type,
        )

    def _get_access(
        self, request: HttpRequest, pk: int
    ) -> tuple[Course, Enrollment | None]:
        """Return the course and the requester's enrolment (owner: ``None``).

        The owner always has access, draft or not.  Everyone else needs a
        published course and an enrolment: a draft course is a 404 so its
        existence is not revealed, and a published course without an enrolment
        is a 403.  The enrolment is returned so the caller can apply the
        lesson-ordering rule without a second query.
        """
        course = get_object_or_404(Course, pk=pk)
        if course.instructor_id == request.user.pk:
            return course, None
        if not course.is_published:
            raise Http404("This course is not available.")
        enrollment = Enrollment.objects.filter(user=request.user, course=course).first()
        if enrollment is None:
            raise PermissionDenied
        return course, enrollment


class MyCoursesView(LoginRequiredMixin, ListView):
    """The learner's enrolments, with progress shown for each.

    ``total_lessons`` and ``completed_lessons`` are annotated in the database,
    so the page stays a constant number of queries no matter how many courses
    the learner has (one query for the enrolments, none per row).
    """

    template_name = "courses/my_courses.html"
    context_object_name = "enrollments"

    def get_queryset(self) -> QuerySet[Enrollment]:
        return enrollments_with_progress(self.request.user)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        for enrollment in context["enrollments"]:
            enrollment.progress = {
                "completed": enrollment.completed_lessons,
                "total": enrollment.total_lessons,
                "percent": progress_percent(
                    enrollment.completed_lessons, enrollment.total_lessons
                ),
            }
        return context
