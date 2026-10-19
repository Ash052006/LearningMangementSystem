# Staff Training LMS

![CI](https://github.com/Ash052006/LearningMangementSystem/actions/workflows/ci.yml/badge.svg)

Internal staff-training site for a legal services organisation. Instructors
create courses (ordered lessons + end-of-course multiple-choice quiz) and see
learner progress; learners browse published courses, enrol, complete lessons
and take quizzes.

Stack: Django 5.x, Python 3.12, SQLite, server-rendered templates, plain
Django forms. Apps: `accounts` (custom user), `courses`, `quizzes`, and `api`
(DRF). Business logic lives in `services.py` modules, not views/templates.

## Prerequisites

- **Python 3.12** (3.11+ should work; developed and tested on 3.12).
- **pip** and **venv**, bundled with Python.
- **Git**, to clone the repository.
- **SQLite**, used for the database; the driver is bundled with Python, so
  there is nothing else to install.
- **gettext** (`msgfmt`, `xgettext`) is only needed if you want to *edit* the
  translations — the compiled `locale/ml/LC_MESSAGES/django.mo` is committed,
  so the site runs without it.
- No JavaScript toolchain, Node, or database server is required.

## Quick start

```bash
# 1. Clone and enter the project
git clone <repository-url>
cd "PUCAR Assignment"

# 2. Create and activate a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure the environment.  SECRET_KEY is required: the project has no
#    fallback key, so generate one and put it in .env
cp .env.example .env
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
#    -> paste the output into .env as SECRET_KEY=<value>

# 5. Create the database schema
python manage.py migrate

# 6. Load the demo accounts, courses, quiz and progress
python manage.py seed_demo

# 7. (optional) an admin account for /admin/
python manage.py createsuperuser

# 8. Run the development server
python manage.py runserver
```

Then open <http://127.0.0.1:8000/>. The site routes by role: instructors land
on their dashboard, learners on the catalogue, and anonymous visitors on the
login page. The Django admin is at `/admin/`.

`seed_demo` is idempotent — run it as often as you like; it never duplicates
data. It resets the demo passwords each run so the credentials below always
work.

## Demo credentials

All three demo accounts use the password **`demo12345`**.

| Role       | Username          | Password    | What to try |
| ---------- | ----------------- | ----------- | ----------- |
| Instructor | `demo_instructor` | `demo12345` | Dashboard, per-course learner progress, CSV export, the draft course |
| Learner    | `demo_learner1`   | `demo12345` | Partial progress and one passing quiz attempt already recorded |
| Learner    | `demo_learner2`   | `demo12345` | Enrolled but no activity, so the dashboard shows an empty learner |

The published demo course is "E-Filing for Court Clerks" (5 lessons + a
5-question quiz). "Advanced E-Filing: Case Management" is a **draft**: learners
get a 404 for it, which demonstrates the visibility rule.

## Tests and lint

```bash
python manage.py test                   # 417 tests in the checked project state
ruff format --check . && ruff check .   # check formatting and lint
```

Config comes from environment variables via django-environ; `.env.example`
documents the variables and `.env` is gitignored.

## Running with Docker

The project ships with a `Dockerfile` and `docker-compose.yml`, so the whole
app (gunicorn + WhiteNoise for static files, SQLite on a volume) runs in one
container:

```bash
docker compose up --build        # build the image and start it
```

Then open <http://localhost:8000/>. The compose file seeds the demo data, so
the accounts below work straight away.

Other useful commands:

```bash
docker compose down               # stop the container
docker compose down -v            # stop it and delete the demo database volume
docker compose exec web sh        # open a shell in the running container
docker build -t staff-training-lms . && docker run --rm -p 8000:8000 staff-training-lms
```

Notes:

- The image is based on `python:3.12-slim`, runs as a non-root `app` user, and
  runs `collectstatic` at build time so WhiteNoise can serve the static files
  (a throw-away build-only `SECRET_KEY` is enough for that step; it is not
  persisted in the image).
- The database lives on the named volume `demo-data`, mounted at `/data` and
  pointed to by `DATABASE_PATH`, so courses and progress survive restarts.
- On start, `docker-entrypoint.sh` runs migrations and (because `SEED_DEMO=1`)
  loads the demo data, then starts gunicorn.
- For a real deployment, provide a strong `SECRET_KEY` (the container will not
  start without one), set `ALLOWED_HOSTS`, and leave
  `ALLOW_INSTRUCTOR_SELF_SIGNUP` at its default `False` unless you deliberately
  want self-service instructor accounts.

Continuous integration runs on every push (`.github/workflows/ci.yml`): ruff
lint/format, `makemigrations --check --dry-run`, and the test suite under
`coverage`. Update the CI badge URL above to your own `OWNER/REPO`.

## Project layout

```
config/            project settings, root URLs, wsgi/asgi
accounts/          custom User model, signup/login/logout, role-based home
courses/           Course, Lesson, Enrollment, LessonProgress; instructor and
                   learner views; services.py (business logic); validators.py
                   (attachment size/type); dashboard stats; seed_demo command
quizzes/           Quiz, Question, Choice, QuizAttempt, AttemptAnswer;
                   authoring, taking, grading and submission services
api/               DRF serializers, views and URLs for the /api/v1/ JSON API
templates/         base + error pages, and one folder per app area
static/css/        a single hand-written stylesheet
locale/            .po/.mo translation catalogs (ml = Malayalam)
manage.py          Django's entry point
requirements.txt   Django, django-environ, DRF, drf-spectacular, gunicorn,
                   whitenoise, ruff, coverage
ruff.toml          lint/format configuration
.coveragerc        scopes coverage to this project's code
Dockerfile         slim Python 3.12 image, non-root user, gunicorn + WhiteNoise
docker-compose.yml run the app with the database on a named volume
docker-entrypoint.sh  migrate (+ optional seed_demo), then exec gunicorn
.github/workflows/ CI: ruff, migration check, tests under coverage
.env.example       documented environment variables (copy to .env)
```

## Key design decisions

- Business rules live in `services.py` modules and are called by thin views, so
  the same logic is shared by web pages, the CSV export and tests.
- Permissions are enforced server-side with mixins and per-object checks;
  drafts are 404 to non-owners so unpublished work is not even revealed.
- Every state change is a POST form with `{% csrf_token %}`; nothing important
  happens over GET.
- Progress is always derived from `LessonProgress` rows, never stored, so it
  cannot drift; quiz attempts and their answers are immutable once written.
- Lesson attachments are private: they are streamed by a permission-checked
  view, never served from the media URL.

---

## Task 1: project scaffold

Django project `config` with apps `accounts`, `courses`, `quizzes`;
custom user model, environ-based settings, base/error templates, role-based
home redirect.

Files:

- `config/settings.py`, `config/urls.py`
- `accounts/models.py`, `views.py`, `urls.py`, `tests.py`,
  `migrations/0001_initial.py`
- `courses/views.py`, `urls.py` · `quizzes/urls.py` (empty namespace)
- `templates/base.html`, `403.html`, `404.html`, `500.html`,
  `accounts/{login,dashboard}.html`, `courses/catalogue.html`
- `static/css/style.css`
- `requirements.txt`, `.env.example`, `.gitignore`, `ruff.toml`,
  `README.md`, `NOTES.md`

Test: `python manage.py test accounts` — user model defaults/role helpers,
home routing per role, role-aware nav, POST+CSRF logout form, error templates.

## Task 2: models and constraints

All nine models with UniqueConstraints, indexes, validators and deliberate
on_delete rules (table in NOTES.md).

Files:

- `courses/models.py` — Course, Lesson, Enrollment, LessonProgress
- `courses/validators.py` (new) — attachment size/extension validators
- `quizzes/models.py` — Quiz, Question, Choice, QuizAttempt, AttemptAnswer
- `courses/tests.py`, `quizzes/tests.py` — constraint, ordering, validator,
  clean() and cascade/protect tests
- `courses/migrations/0001_initial.py`, `quizzes/migrations/0001_initial.py`

Test: `python manage.py test courses quizzes` — 54 tests (IntegrityError on
duplicate constraints, position ordering, attachment validators,
LessonProgress.clean() course match, PROTECT/SET_NULL behaviour).

---

## Task 3: Django admin

All models registered usefully: course page with an ordered lesson inline and
lesson/enrolment counts; quiz page with question inline; question page with
choice inline; enrolment and progress changelists filterable by course with
date drill-down; quiz attempts and answers read-only; user admin extended
with role.

Files:

- `courses/admin.py` — CourseAdmin (annotated counts, `list_filter`,
  `search_fields`, LessonInline), LessonAdmin, EnrollmentAdmin,
  LessonProgressAdmin
- `quizzes/admin.py` — QuizAdmin (QuestionInline), QuestionAdmin
  (ChoiceInline), ReadOnlyAdmin + QuizAttemptAdmin, AttemptAnswerAdmin
- `accounts/admin.py` — UserAdmin extending `BaseUserAdmin` with role in
  `list_display`, `list_filter`, fieldsets and add_fieldsets
- `config/tests.py` (new) — project-level admin integration tests

Test: `python manage.py test config` — every registered changelist returns
200 for a superuser, read-only rules are enforced server-side, read-only and
inline-host change pages load, and changelist query counts stay constant as
rows are added (no N+1).

---

## Task 4: signup, login and logout

Self-service signup, login and logout built on Django's auth views, with a
labelled form, per-field error messages, help text and success messages.

Files:

- `accounts/forms.py` — `SignUpForm(UserCreationForm)` (username, email,
  password1/password2, role)
- `accounts/views.py` — `SignUpView`, `LoginView` (subclass of Django's
  `LoginView` adding a welcome message), `DashboardView`; `HomeRedirectView`
  unchanged
- `accounts/urls.py` — `signup/`, `login/`, `logout/` (built-in `LogoutView`)
- `templates/accounts/signup.html`, `templates/accounts/login.html` and the
  shared partials `_field.html` / `_form_errors.html`
- `templates/base.html` — anonymous nav now links to signup as well as login
- `static/css/style.css` — form, help-text, field-error and button styles
- `config/settings.py` / `.env.example` — `ALLOW_INSTRUCTOR_SELF_SIGNUP`
  (off by default; the signup form hides the role field and forces the learner
  role while it is off)

Behaviour:

- Signup creates the account but does not log the user in; it redirects to the
  login page with a success message.
- Signup always creates a **learner**. The instructor role is granted by an
  administrator in Django admin, unless `ALLOW_INSTRUCTOR_SELF_SIGNUP=True`
  turns on self-service instructor accounts for a demo.
- Login honours a safe `?next=` (validated by Django's `LoginView` against
  `url_has_allowed_host_and_scheme`) and otherwise redirects to `home`.
- Logout is POST-only (Django 5 default): a GET returns 405, so the nav uses a
  CSRF-protected form.

Test: `python manage.py test accounts` — form rendering, the disabled-by-default
instructor switch (field hidden, and a forged instructor role forced to
learner), the opt-in path with the setting enabled, duplicate username and
password-mismatch rejection, safe-vs-external `next`, login/logout messages and
the logout 405.

---

## Task 5: instructor course and lesson CRUD

Instructors manage only their own courses from `/courses/manage/`: list,
create, view, edit and delete courses, publish/unpublish them, and add, edit,
reorder and delete the lessons inside them. Access is enforced with mixins;
ownership is enforced by scoping every queryset to the request user.

Files:

- `accounts/mixins.py` (new) — `InstructorRequiredMixin` (login required, role
  instructor, else 403) and `OwnerCourseMixin` (`get_queryset` returns
  `Course.objects.filter(instructor=request.user)`)
- `courses/forms.py` (new) — `CourseForm` (title, description, enforce_order)
  and `LessonForm` (title, content, video_url, attachment)
- `courses/services.py` (new) — `toggle_course_publish`, `resequence_lessons`,
  `move_lesson_up` / `move_lesson_down`
- `courses/views.py` — instructor course list/detail/create/update/delete/
  toggle and nested lesson create/update/delete/move views
- `courses/urls.py` — the `manage/...` URL tree
- `templates/courses/instructor/*.html` — course list (status badges), course
  detail (ordered lessons with Up/Down/Edit/Delete), course and lesson forms,
  and delete-confirmation pages
- `templates/partials/_field.html`, `_form_errors.html` — shared field/error
  rendering, promoted from `templates/accounts/` so courses reuse it
- `templates/base.html` — instructor nav gains a "My courses" link
- `static/css/style.css` — badges, data table, lesson list and button variants

Behaviour:

- Creating a course sets `instructor` from `request.user`; the field is not in
  the form, so a posted `instructor` value is ignored.
- Publishing requires at least one lesson; an empty course shows a clear error
  and stays draft. Unpublishing always succeeds.
- Lesson delete renumbers the remaining lessons to 1..n, so positions stay
  contiguous; move up/down swaps neighbours inside `transaction.atomic`.
- Lesson `video_url` accepts only http/https; attachments obey the 10 MB and
  extension validators.
- Every action posts with CSRF and flashes a success or error message.

Test: `python manage.py test courses` — learner gets 403 on course
create/edit/delete/list/publish; a second instructor gets 404 on another
instructor's course and lesson URLs; create ignores a posted instructor;
position swap and delete-resequencing; cannot publish an empty course; GET on
toggle/move returns 405; http/https accepted and ftp rejected.

---

## Task 6: learner-facing flow

Learners browse the published catalogue, open a course, enrol, work through
lessons and mark them complete, and track progress on "My courses".

Files:

- `courses/services.py` — `enrol` (idempotent, race-safe), `mark_lesson_complete`
  (idempotent), `progress_percent` and `course_progress`
- `courses/views.py` — `CatalogueView` (published only, paginated, annotated
  lesson counts), `LearnerCourseDetailView`, `EnrolView`, `LearnerLessonView`,
  `LessonCompleteView`, `MyCoursesView`
- `courses/urls.py` — learner routes at `/courses/`, `/courses/my/`,
  `/courses/<pk>/`, `/courses/<pk>/enrol/` and the lesson URLs
- `templates/courses/catalogue.html`, `course_detail.html`, `lesson.html`,
  `my_courses.html` and the shared `_progress.html`
- `templates/partials/_pagination.html` (new)
- `templates/base.html` — learner nav gains a "My courses" link
- `static/css/style.css` — course cards, progress bar, pagination, lesson nav

Behaviour:

- The catalogue lists published courses only, newest first, nine per page,
  with lesson counts annotated in SQL (no N+1).
- Course detail is `get_object_or_404`-style published-only for newcomers,
  shows the description and lesson titles, and offers an Enrol button (POST)
  when the learner is not enrolled; once enrolled it shows progress and a
  Continue link to the first unfinished lesson.
- A lesson requires enrolment; a non-enrolled learner is redirected to the
  course page with a message. Content is autoescaped and rendered with
  `linebreaks`; the optional video and attachment become links; previous/next
  navigation and a Mark complete button (POST) are shown.
- "My courses" lists enrolments with "4 of 7 lessons done (57%)" and a
  `<progress>` bar; completed/total are annotated so the page is a constant
  number of queries.
- Unpublish policy: withdrawing a course hides it from the catalogue and
  blocks *new* enrolments, but learners who already enrolled keep access to
  their history (documented in `LearnerCourseAccessMixin`).

Test: `python manage.py test courses` — draft course 404s on detail, lesson and
enrol URLs; a non-enrolled learner cannot open a lesson; double enrol and
double complete each count once; progress rounding and the zero-lesson case;
`assertNumQueries` proves the catalogue and "My courses" query counts are
constant as rows grow.

---

## Task 7: instructor quiz authoring

An instructor builds the quiz for a course they own on one page: quiz settings,
the questions, and each question's choices.

Files:

- `quizzes/forms.py` — `QuizForm`, `QuestionForm`, `ChoiceForm` and the two
  inline formsets; `BaseChoiceFormSet` enforces the per-question rules
- `quizzes/services.py` — `question_has_attempts`, `resequence_questions`,
  `move_question_up`/`move_question_down`
- `quizzes/views.py` — `QuizEditorView` (create or edit), the POST-only
  `QuestionMoveUpView`/`QuestionMoveDownView` and `QuestionDeleteView`
- `quizzes/urls.py` — `quizzes:quiz_edit` and the question move/delete routes
- `quizzes/models.py` — `Quiz.has_questions` property
- `templates/quizzes/quiz_edit.html` — the nested form editor
- `templates/courses/instructor/course_detail.html` — quiz section and link
- `static/css/style.css` — notices, question blocks and the choice table
- `courses/views.py` — the publish flow warns when the quiz has no questions

Behaviour:

- The editor is a single `<form>`: `quiz_form` (pass_mark, optional
  max_attempts, show_answers), a question inline formset, and — nested inside
  each question — a choice inline formset. Each question renders six choice
  slots; existing choices are shown with a delete checkbox, and blank slots are
  ignored.
- A question needs between 2 and 6 choices and exactly one marked correct;
  question and choice text must be non-blank. A question row with choices but
  no question text is rejected rather than silently dropped. Nothing is saved
  while any part of the form is invalid.
- Questions can be reordered (up/down) and deleted through POST-only endpoints.
  Reordering at the top/bottom is a no-op with a message.
- If the quiz already has attempts, the editor shows a clear warning; attempts
  are never modified or deleted. Deleting a question that has attempts is
  refused with an explanation (its `AttemptAnswer` rows are `PROTECT`), while
  deleting an unanswered question works and renumbers the rest.
- A quiz with no questions is saved but hidden from learners; publishing a
  course whose quiz is empty succeeds and warns that the quiz will not be shown
  until it has a question (`Quiz.has_questions`). A quiz with questions does not
  trigger the warning.
- Access is owner-only, reusing `OwnerCourseMixin`: anonymous users are
  redirected to login, non-instructors get 403 (the established role rule) and
  another instructor gets 404 on every quiz URL.

Test: `python manage.py test quizzes` — choice-formset validation (zero correct,
two correct, one choice, more than six, valid two and six), question/choice
creation and editing through the view, blank question text rejected, reorder
and at-edge no-op, POST-only endpoints (GET is 405), delete blocked when a
question has attempts (and the attempt untouched), the attempts warning, the
publish-visibility warning, and role/ownership checks.

---

## Task 8: quiz taking and server-side grading

An enrolled learner answers the course quiz; every submission is graded on the
server and stored as an immutable attempt. Instructors can run the same flow as
a save-nothing preview.

Files:

- `quizzes/services.py` — `grade_attempt` (pure), `submit_attempt`,
  `GradeResult`, `answers_from_post`, plus `QuizNotGradableError` and
  `AttemptsExhaustedError`
- `quizzes/views.py` — `TakeQuizView` (GET form, POST grades),
  `QuizResultView`, `QuizPreviewView` and the `QuizLearnerMixin`
- `quizzes/urls.py` — `quizzes:take`, `quizzes:result` and `quizzes:preview`
- `templates/quizzes/take_quiz.html` — radio-button question form (no
  correctness information)
- `templates/quizzes/quiz_result.html` — verdict, score, history, review
- `templates/courses/course_detail.html` — "Take the quiz" link once ready
- `static/css/style.css` — notice variants, choice options and review styles

Behaviour:

- `grade_attempt(quiz, answers)` is a pure function: the correct choices are
  read from the database, never trusted from the request. A choice id that does
  not belong to its question and a question id that is not in the quiz are
  ignored; an unanswered question counts as wrong. It returns a `GradeResult`
  with `score`, `total`, `percent` (two decimals), `passed` and `per_question`;
  a quiz with no questions raises `QuizNotGradableError`.
- `passed` is exactly `percent >= quiz.pass_mark`, so 7 of 10 at a 70% pass mark
  passes and 69% fails.
- `submit_attempt(enrollment, quiz, post_data)` writes the `QuizAttempt` and
  one `AttemptAnswer` per question inside `transaction.atomic`. It sanitises
  submissions the same way as grading, and when `max_attempts` is set it locks
  the enrolment and counts existing attempts first, raising
  `AttemptsExhaustedError` (and writing nothing) when the learner is out of
  attempts. Attempts are only ever created, never updated or overwritten.
- GET shows the questions with radio buttons and never includes correctness
  information. POST grades and redirects to a result page (POST/redirect/GET,
  so a refresh cannot resubmit).
- The result page shows score, percentage, pass/fail, the pass mark and the
  attempt history, and shows the answer review only when `quiz.show_answers` is
  true. A retake link is shown only while attempts remain.
- Access: login required; the learner must be enrolled (403 otherwise); a draft
  course is a 404 (unlike lesson access, the quiz flow closes with the course);
  another learner's attempt is a 404. Instructors preview their own quiz from an
  owner-scoped route that grades but never saves.

Test: `python manage.py test quizzes` — all correct / all wrong / partial /
unanswered, the exact pass-mark boundary (70% passes, 69% fails), foreign choice
and tampered question ids ignored, a zero-question quiz, `max_attempts`
enforcement, attempts accumulating without being overwritten, and the view
rules: `is_correct` never in the GET HTML, non-enrolled 403 on GET and POST,
anonymous redirected to login, draft 404, review gated on `show_answers`,
retake only while attempts remain, and the preview saving nothing.

---

## Task 9: instructor dashboard per course

An instructor gets an index of their courses with enrolment counts, and a
per-course dashboard with a learner table, summary totals, per-question
analytics and a CSV export of the same rows.

Files:

- `courses/services.py` — `course_learner_stats(course)` (one `LearnerStat`
  per enrolled learner) and `course_question_stats(course)` (one
  `QuestionStat` per question), plus the two value objects
- `courses/views.py` — `InstructorDashboardView` (index),
  `CourseDashboardView` (per-course), `CourseDashboardCsvView` (download) and
  the `_csv_safe` helper
- `courses/urls.py` — `courses:instructor_dashboard`,
  `courses:course_dashboard`, `courses:course_dashboard_csv`
- `templates/courses/instructor/dashboard.html` — course index with counts
- `templates/courses/instructor/course_dashboard.html` — learner table,
  summary and analytics
- `static/css/style.css` — `.stat-summary` styles
- links added on the accounts dashboard and the course management page

Behaviour:

- `course_learner_stats` returns one row per enrolment with username,
  enrolled-at, lessons completed, total lessons, percent, best quiz percent,
  passed and attempts count — all from a **single** annotated query.
  `Count(..., distinct=True)` and `Max` stop the lesson, progress and attempt
  joins from inflating each other, and `select_related("user")` keeps the query
  count flat as learners are added.
- `course_question_stats` annotates each question with its answer count and
  the number answered correctly (a filtered `Count`), turning them into a
  whole-number percent-correct. A course without a quiz returns no rows.
- The per-course page shows the learner table (sorted by username), summary
  totals (enrolled, completed all lessons, passed the quiz) and the
  per-question analytics. Every figure is computed by the same two services, so
  the page and the CSV cannot drift apart.
- CSV export streams the same rows with a `Content-Disposition: attachment`
  header. A cell whose first meaningful character (after any leading
  whitespace/control characters) is `=`, `+`, `-` or `@` is prefixed with a
  single quote so a spreadsheet treats it as text, neutralising formula
  injection from a malicious username without escaping the numeric columns.
- Permissions: only the owning instructor. A learner gets 403
  (`InstructorRequiredMixin`), another instructor gets 404 (`OwnerCourseMixin`
  scopes the lookup), and anonymous visitors are redirected to login.

Test: `python manage.py test courses` — hand-computed figures across several
learners and attempts, no join inflation with three lessons and two attempts,
a constant query count for `course_learner_stats` and for the dashboard page as
learners grow, another instructor 404 and learner 403 on the page and the CSV,
and CSV neutralisation (a username beginning with `=` plus each dangerous
prefix).

---

## Task 10: private lesson attachments

Lesson attachments are no longer public. The template links to a
permission-checked download view and the media URL is no longer routed at all.

Files:

- `courses/validators.py` — `ATTACHMENT_CONTENT_TYPES` (the single allowlist),
  `ALLOWED_ATTACHMENT_EXTENSIONS` derived from it, and
  `attachment_content_type(filename)` which returns a MIME type or `None`
- `courses/views.py` — `LessonAttachmentView`
- `courses/urls.py` — `courses:lesson_attachment`
  (`courses/<pk>/lessons/<lesson_pk>/attachment/`)
- `templates/courses/lesson.html` — links to the view instead of
  `lesson.attachment.url`
- `config/urls.py` — the `DEBUG`-only `static(MEDIA_URL, ...)` route is removed

Behaviour:

- The view allows the course owner always; otherwise the course must be
  published and the user enrolled, else the response is a 404 (draft, so
  existence is hidden) or a 403 (published but not enrolled). Anonymous
  visitors are redirected to login.
- The file is opened through the storage backend and streamed with
  `FileResponse(as_attachment=True, ...)`. The content type comes from the
  extension allowlist, never from guessing, so a stored file cannot be served
  as active content. A lesson with no attachment, a disallowed extension, or a
  file that has gone missing from disk all return 404.
- `Content-Disposition` is built by Django from `os.path.basename(...)`, so
  non-ASCII names are encoded per RFC 5987 and CR/LF cannot be injected into
  the header.

In production the same checks can front an `X-Accel-Redirect` internal
redirect (nginx serves the bytes) or a short-lived pre-signed object-storage
URL, so Django never proxies large files while the permission check stays in
one place. See NOTES.md.

Test: `python manage.py test courses` — `LessonAttachmentTests` covers an
enrolled learner downloading (correct type, disposition and bytes), the owner
downloading a draft, draft/enrolled 404s, non-enrolled and rival-instructor
403s, anonymous redirect with `?next=`, a lesson without an attachment, a
disallowed extension, a missing file, the lesson page linking to the view (not
the media URL), and `/media/...` being unreachable.

---

## Task 11: demo data and documentation

A single idempotent management command builds everything needed to explore the
site, and the README/NOTES were completed.

Files:

- `courses/management/commands/seed_demo.py` — the demo seeder
- `README.md` — prerequisites, quick start, demo credentials, layout, design
  decisions
- `NOTES.md` — tools/AI used, data model rationale, assumptions, what was left
  out, time spent

Behaviour: `seed_demo` creates three accounts (`demo_instructor`,
`demo_learner1`, `demo_learner2`), a published "E-Filing for Court Clerks"
course (5 lessons + a 5-question quiz), a draft course, and gives
`demo_learner1` two completed lessons and one passing attempt. It uses
`get_or_create` throughout (the attempt is guarded by an existence check), so
running it twice changes nothing; only the demo passwords are reset so the
printed credentials always work. It prints the credentials table at the end.

Test: `python manage.py test courses` — `SeedDemoCommandTests` asserts the
accounts, courses, lessons, questions, progress and attempt are created, that
the printed password authenticates, and that a second run duplicates nothing.

---

## Task 12: catalogue search, instructor filter, sorting and pagination

The learner catalogue can now be searched and narrowed. Every GET parameter is
validated by a Django form, and the draft rule is unchanged.

Files:

- `courses/forms.py` — `CatalogueFilterForm` (`q`, `instructor`, `sort`,
  `page`), plus the `SORT_NEWEST` / `SORT_TITLE` constants
- `courses/views.py` — `CatalogueView` reads the form, builds the queryset and
  paginates from the validated page number
- `templates/courses/catalogue.html` — the filter bar
- `templates/partials/_pagination.html` — page links keep the active filters
- `static/css/style.css` — the `.catalogue-filters` bar

Behaviour:

- Search is a case-insensitive `icontains` over the title and the description.
  The base queryset is always filtered to `PUBLISHED`, so no search term can
  ever surface a draft.
- The instructor filter is a `ModelChoiceField` of instructors; sorting is
  either newest first (`-created_at, -pk`) or by title (`title, pk`).
- `CatalogueFilterForm` is bound to `request.GET`. Invalid or missing values are
  dropped and the default is used, so e.g. `?page=abc`, `?sort=sideways`,
  `?q=` (500 characters) or `?instructor=999999` return the page rather than a
  500. Out-of-range pages are clamped by `Paginator.get_page`.
- Pagination links are re-encoded from the validated filters, so moving between
  pages keeps the search, the instructor and the sort.

Test: `python manage.py test courses` — `CatalogueFilterTests` covers search by
title and description, case-insensitivity, drafts never appearing (by
description token and by title), the instructor filter, both sorts, eight
invalid-parameter cases, page clamping, filter-preserving page links and the
instructor dropdown; `LearnerQueryCountTests` pins the query count as filtered
result sets grow.

---

## Task 13: enforcing lesson order

When a course sets `enforce_order`, a lesson opens only once every lesson before
it is complete. The rule is enforced on the server, not just by hiding links.

Files:

- `courses/services.py` — `unlocked_lesson_ids`, `can_access_lesson` and
  `first_blocking_lesson`
- `courses/views.py` — the learner mixin, lesson view, complete view and course
  detail all use the rule
- `templates/courses/course_detail.html` — locked rows with the reason
- `templates/courses/lesson.html` — the Next link waits for the gate
- `static/css/style.css` — `.tag--locked` / `.locked-reason`

Behaviour:

- `can_access_lesson(enrollment, lesson)` is the single source of truth: with the
  flag off, every lesson is open; with it on, a lesson is open when every earlier
  lesson is complete (the first incomplete lesson opens; the rest lock). An
  already-completed lesson stays readable wherever it sits.
- Both the lesson GET and the mark-complete POST call it, so a learner who types
  a later lesson's URL or POSTs to its complete endpoint is sent back to the
  course with a message naming the lesson that gates it.
- Reordering or inserting lessons cannot break access: the rule reads positions
  live, so the lock always follows the current order.
- A previewing instructor is exempt: the owner may open any lesson of their own
  course (drafts included) without enrolling, in a read-only preview that records
  no progress.

Test: `python manage.py test` — `LessonOrderEnforcementTests`,
`OrderServiceTests` and `PreviewModeTests` cover skipping ahead by URL and by
POST, unlocking in order, the outline reason, the hidden Next link, toggling the
flag off, reordering, inserting, and instructor preview.

---

## Task 14: completion certificates

A learner who finishes a course earns a certificate, printable and publicly
verifiable by its code.

Files:

- `courses/models.py` — `Certificate` (one per enrolment, random `code`,
  `issued_at`)
- `courses/services.py` — `certificate_eligible` / `issue_certificate_if_eligible`
- `courses/views.py` — `CertificateIssueView` (POST), `CertificateView` (GET
  printable) and `CertificateVerifyView` (public)
- `courses/urls.py` / `config/urls.py` — the learner certificate routes and the
  public `/certificates/<uuid>/` route
- `templates/courses/course_detail.html` — the "Download certificate" button
- `templates/courses/certificate.html` — the printable page
- `templates/courses/verify_certificate.html` — the public verification page
- `static/css/certificate.css` — the print stylesheet

Behaviour:

- Eligibility is "every lesson complete **and** the quiz passed", where a course
  with no quiz (or a quiz with no questions) needs no quiz. A course with no
  lessons is never eligible. `issue_certificate_if_eligible` uses
  `get_or_create` keyed on the enrolment, so it is idempotent and race-safe.
- Once issued, a certificate is **never revoked**. Eligibility is only checked
  when the row is first created, so adding a lesson to the course afterwards
  does not invalidate an existing certificate — it only stops a *new* one being
  issued. (Tested: `test_issued_certificate_survives_a_new_lesson`.)
- The course page shows a "Download certificate" button only when the learner is
  eligible, or already holds one (so an issued certificate stays reachable).
  Issuing is a POST (the project's POST-only rule) which then redirects to the
  printable GET page.
- The printable page shows the learner's name, the course, the issue date and
  the code, and has print CSS that hides the site chrome so only the
  certificate prints.
- `/certificates/<uuid>/` is public (no login). A valid code shows the holder
  name, course, date and code — the facts already printed on the certificate —
  and an unknown code renders a "not found" message with a 404. Nothing else
  (email, username, enrolment, progress) is exposed.

Test: `python manage.py test` — `CertificateServiceTests` (eligibility, the
no-quiz / no-questions / no-lessons rules, idempotency, survival after a new
lesson, holder name), `CertificateViewTests` (button gating, POST-only issuing,
the printable page, access control, survival after a new lesson) and
`CertificateVerifyTests` (valid, unknown, non-UUID and no-private-data).

## Task 15: accessibility audit

Every template was reviewed against a fixed checklist (bound labels, quiz
`fieldset`/`legend`, focus styles, skip link, landmarks, `aria-live`, scoped
table headers, contrast, no colour-only meaning, keyboard-only operation). The
audit and the fixes:

- **Already correct, left alone.** Forms render labels with `field.label_tag`
  (`templates/partials/_field.html`); the quiz radio groups are `fieldset` +
  `legend`; flash messages are `role="status" aria-live="polite"`; field and
  non-field errors carry `role="alert"`; data tables have a
  `caption.visually-hidden` and `th scope="col"`; the skip link, `<header>`,
  `<nav aria-label>`, `<main>` and `<footer>` give the expected landmarks; every
  status badge also says "Published"/"Draft"/"Passed"/"Failed"/"Locked" in text,
  so nothing relies on colour alone.
- **Contrast.** All foreground/background pairs pass WCAG AA; the tightest is
  muted text on the surface colour (`#52606d` on `#f5f7fa`, 6.0:1).
- **Fixed.** (1) The skip link's target `<main>` gained `tabindex="-1"` so
  activating the link actually moves keyboard focus. (2) A global
  `:focus-visible` outline was added — keyboard focus is now visible on inputs,
  links and buttons, while mouse clicks stay clean. (3) The quiz question text
  moved into the group `<legend>`, so screen readers announce it as the group
  name rather than a stray paragraph. (4) The catalogue filter form is now a
  `role="search"` landmark with an `aria-label`. (5) Repeated row actions
  (Edit/Publish on the course list, Up/Down/Edit/Delete on a course's lessons,
  View progress/CSV on the dashboard, View in the attempt history) carry a
  visually-hidden course/lesson/attempt name so screen-reader users can tell
  them apart. (6) Video links that open a new tab say so ("opens in a new tab").
  (7) Per-course progress bars in the "my courses" list have distinct
  `aria-label`s instead of all reading "Course progress".
- **Left as-is.** The certificate "Print" button uses a tiny inline
  `window.print()` call; it is a real `<button>` (fully keyboard operable) and
  printing still works from the browser menu without JavaScript.

Test: `python manage.py test` — `AccessibilityAuditTests` locks in the fixes
(skip-link target, search landmark, distinct progress labels, question legend,
named row actions, new-tab notice).

---

## Task 16: JSON API (/api/v1/)

A small DRF API over the same models. It adds **no new business rules**: it
reuses `courses/services.py` (`published_courses`, `enrol`,
`enrollments_with_progress`, `mark_lesson_complete`, `can_access_lesson`,
`first_blocking_lesson`, `course_progress`) and the shared visibility helper
`courses/access.py:ensure_course_visible`, so the API and the web pages cannot
drift apart.

| Method | Path | What it does |
| ------ | ---- | ------------ |
| GET  | `/api/v1/courses/` | Published courses, newest first, paginated (10/page) |
| GET  | `/api/v1/courses/<id>/` | One course with its ordered lessons |
| POST | `/api/v1/courses/<id>/enrol/` | Enrol the current user (idempotent) |
| GET  | `/api/v1/my/enrolments/` | The caller's enrolments with derived progress |
| POST | `/api/v1/courses/<id>/lessons/<lesson_pk>/complete/` | Mark a lesson complete |
| POST | `/api/v1/auth/token/` | Exchange username/password for an API token |

- **Auth.** `TokenAuthentication` and `SessionAuthentication` are both enabled;
  the token is tried first so an unauthenticated request gets a proper `401`
  with a `WWW-Authenticate: Token` header. Every endpoint needs a logged-in
  user (`IsAuthenticated`).
- **Same access rules (R7).** An unpublished course is a `404` for anyone who
  is neither its owner nor already enrolled — including via the API. Marking a
  lesson complete without an enrolment is `403`; a course the user is hidden
  from is likewise `404`.
- **Lesson order.** `POST .../complete/` re-checks `can_access_lesson` before
  writing, so calling the API directly cannot skip ahead. A blocked lesson is
  `409 Conflict` (the lesson is allowed, the order is not) and the message
  names the lesson to finish first. Completing a lesson twice is idempotent:
  `201` the first time and `200` after, never a duplicate row.
- **Pagination and throttling.** DRF's `PageNumberPagination` at 10 per page;
  `AnonRateThrottle` 30/min and `UserRateThrottle` 120/min.
- **Docs.** The OpenAPI 3 schema is at `/api/schema/` and Swagger UI at
  `/api/docs/` (`drf-spectacular`).

"instructor-only writes" is kept true by *not* exposing authoring: the course
collection and detail are read-only (`405` on any unsafe method), so creating
or editing courses/lessons stays on the web where `InstructorRequiredMixin` and
`OwnerCourseMixin` guard it. The only writes over the API are the learner
actions (enrol, mark complete).

Test: `python manage.py test api` — 32 tests via `APIClient` covering both auth
methods, the token endpoint, published-only listing and pagination, the draft
`404` rule (learner and rival instructor; owner and enrolled still see it),
enrol idempotency, progress in `my/enrolments/`, the complete-lesson flow
(`403`/`404`/`409`/idempotency), read-only course endpoints, throttling (`429`)
and the served schema.

---

## Task 18: internationalisation (English + Malayalam)

The site is wired for i18n and ships with a Malayalam translation of every
message.

- **Settings** (`config/settings.py`): `LANGUAGE_CODE = "en"`, `LANGUAGES =
  [("en", "English"), ("ml", "Malayalam")]` and `LOCALE_PATHS = [BASE_DIR /
  "locale"]`. `LocaleMiddleware` sits in its documented position (after
  `SessionMiddleware`, before `CommonMiddleware`), and
  `django.template.context_processors.i18n` is added to `TEMPLATES` so
  templates can ask which languages exist and which one is active.
- **Switcher** (`templates/base.html`): a small form in the nav posts to
  `{% url 'set_language' %}` (`/i18n/setlang/`, from `django.conf.urls.i18n`
  included in `config/urls.py`). Choosing a language is a state change — it
  sets a cookie — so it is a `POST` with `{% csrf_token %}` and a hidden
  `next` back to the current page, never a plain link. `<html lang>` renders
  `{{ LANGUAGE_CODE }}` instead of a hardcoded `en`.
- **Translations**: `locale/ml/LC_MESSAGES/django.po` covers all **285**
  extracted messages, including both plurals (`nplurals=2; plural=(n != 1)`),
  and the compiled `django.mo` is committed so nothing needs gettext at run
  time. To refresh after changing strings:
  `python manage.py makemessages -l ml`, edit the `.po`, then
  `python manage.py compilemessages -l ml`.
- **Terminology and review notes** (which words are assumed, which need a
  native speaker's eye) are in NOTES.md under Task 18.

Test: `python manage.py test config` — `MalayalamLanguageTests` asserts the
English default, a Malayalam render from `Accept-Language`, the switcher POST
setting the cookie and marking `ml` selected, and Malayalam plurals on the
catalogue. Full suite: 402 tests.

---

## Task 19: security fixes (first batch)

A security review of the finished project produced a severity table of
findings. This task fixes the first four that were approved — two high, two
medium — and nothing else; the rest are still open (see NOTES.md).

| # | Issue | Fix |
|---|-------|-----|
| H-1 | Any visitor could create an **instructor** account: the signup form offered the role, and the project shipped with self-signup enabled, so an attacker could self-grant course-authoring powers. | `ALLOW_INSTRUCTOR_SELF_SIGNUP` now defaults to **`False`** (`config/settings.py`). With it off the form hides the `role` field and `SignUpForm.save()` forces the learner role, so a crafted `role=instructor` POST still creates a learner. |
| H-2 | `SECRET_KEY` had a hard-coded fallback in `config/settings.py`. Any deployment that forgot to set it would sign sessions, cookies and CSRF tokens with a value published in the repository — enough to forge a session for any user. | The fallback is gone. Settings read `SECRET_KEY` from the environment (or `.env`) and raise `ImproperlyConfigured` when it is missing **or empty**, so the process cannot start insecure. `.env.example`, README, the Dockerfile (build-only `ARG` for `collectstatic`) and CI (generates a throw-away key per run) were updated to match. |
| M-1 | `/accounts/dashboard/` was reachable by any logged-in learner and rendered instructor-only information (course counts, authoring links). | `DashboardView` now mixes in `InstructorRequiredMixin`: anonymous visitors are redirected to login with `?next=`, authenticated non-instructors get `403`. |
| M-2 | A learner who had not yet unlocked a lesson could still download that lesson's attachment from its direct URL, bypassing lesson-order enforcement. | `LessonAttachmentView` applies the same gate as the lesson page and the mark-complete POST: the owning instructor still previews freely, but a learner is redirected to the course page with the usual "complete X first" message. The message text is now built by one shared helper (`locked_lesson_message`) used by all three call sites. |

Notes:

- H-2 was deliberately implemented as a *required, non-empty* setting rather
  than value-blacklisting; `.env.example` no longer ships a usable key (the
  `SECRET_KEY` line is commented out with the command to generate one), so the
  documented setup path never ends up running with a placeholder.
- `docker-compose.yml` still passes a placeholder key for the local demo stack.
  That is a separate, already-known finding (M-5) and was left untouched here.

Test: `python manage.py test accounts config courses` — new regressions cover
the dashboard roles (`DashboardAccessTests`), the signup default and the opt-in
path (including a forged `role=instructor`), the required `SECRET_KEY` (missing,
empty and valid, imported in a subprocess with the local `.env` removed), and
lesson ordering on the attachment URL. Full suite: **417 tests**, coverage 99%.

---

## Next tasks

Remaining work (see NOTES.md for placeholders and pending decisions): any
notification features, and the security findings not included in the first
batch (M-3–M-5, L-1–L-4, I-1–I-5). The instructor dashboard/reporting, CSV
export, private attachments, demo seeding, catalogue search/filter/sort,
lesson-order enforcement, completion certificates, the accessibility pass, the
JSON API, Docker/CI, English/Malayalam i18n and the first security batch are
done in Tasks 9–19.
