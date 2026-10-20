# PUCAR Staff Training LMS — Project Guide

This guide describes the implementation in this checkout. **Verified** means
confirmed in code/tests; any rationale is labelled **inferred**, not claimed as
the original author's recorded intent. Examples are illustrative unless called
seeded demo data.

## 1. Overview

PUCAR is an internal LMS for instructors to publish courses made of ordered
lessons and an end-of-course multiple-choice quiz. Learners find published
courses, enrol, study, record completion, take quizzes, see progress and earn
completion certificates. Instructors manage only their own courses and view
learner and question analytics.

The site uses Django server-rendered templates and a JSON REST API. The API
reuses course access/progress rules but has no authoring or quiz endpoints.
Presentation uses `static/css/style.css` and `static/css/app.css`, with
role-aware navigation, responsive layouts and decorative inline SVG artwork;
there is no JavaScript framework or build step. The interface refresh changed
presentation only—not models, routes, APIs, forms, submissions or permission
behavior. The checked-in Docker files are examples, not a live deployment.

## 2. User workflows

### Instructor

1. Sign in at `/accounts/login/`; signup at `/accounts/signup/` creates a
   learner and does not log them in. An administrator grants instructor role
   in `/admin/`. Self-selection is an opt-in (`ALLOW_INSTRUCTOR_SELF_SIGNUP`),
   disabled by default.
2. `/` sends instructors to `/accounts/dashboard/`, which links to analytics
   (`/courses/manage/dashboard/`) and course management
   (`/courses/manage/`).
3. Create a course at `/courses/manage/new/`. `CourseCreateView` assigns its
   owner from the authenticated request; owner and publication status are not
   accepted from submitted form fields. New courses are drafts.
4. Add/edit lessons from the course page. Positions are assigned automatically;
   Up/Down POST actions reorder through `courses.services`.
5. Edit the course's single quiz at `/quizzes/manage/<course_id>/`. Each
   question requires 2–6 choices and exactly one correct choice. Question
   reorder/delete actions are POSTs.
6. Preview lessons/quizzes without recording learner progress/attempts.
   Publishing is POST-only and requires at least one lesson.
7. Review learner completion, quiz attempts and question analytics at
   `/courses/manage/<course_id>/dashboard/`; export that course's learner
   rows as CSV at `/courses/manage/<course_id>/dashboard/learners.csv`.

### Learner

1. After login, `/` sends learners to `/courses/`. The catalogue shows published
   courses, searches title/description, filters by instructor, sorts by newest
   or title and paginates 9 per page.
2. `/courses/<course_id>/` shows the overview. Enrolment is a CSRF-protected
   POST; a unique `(user, course)` constraint prevents duplicates.
3. Study at `/courses/<course_id>/lessons/<lesson_id>/`. Plain text is
   autoescaped and line breaks become paragraphs. Optional video is an
   external link; attachments use an authorized download view. Completion is
   POST-only. With `enforce_order`, the server checks lesson access on page,
   completion and attachment requests; hiding a link is not the protection.
4. Take the published course quiz at `/quizzes/course/<course_id>/`.
   Server-side grading creates a separate saved attempt per submission.
   `/courses/my/` shows enrolments and derived lesson progress.
5. After completing all lessons and passing a populated quiz (if present), a
   learner may POST to issue a printable certificate at
   `/courses/<course_id>/certificate/`. Its random UUID is publicly verifiable
   at `/certificates/<uuid>/`.

**Unpublishing policy:** the course disappears from the catalogue and new
enrolment is blocked, but existing learners retain course/lesson visibility
and stored history. Quiz-taking/results and attachment downloads for
non-owners currently require publication, so those functions stop working
after unpublishing.

### Course content fields and storage

The instructor UI—not Django admin—is the normal authoring route:

| Operation | Route / implementation |
|---|---|
| List courses | `/courses/manage/` — `InstructorCourseListView` |
| Create course | `/courses/manage/new/` — `CourseCreateView`, `CourseForm` |
| Manage course | `/courses/manage/<course_id>/` — `CourseDetailView` |
| Create lesson | `/courses/manage/<course_id>/lessons/new/` — `LessonCreateView`, `LessonForm` |
| Edit lesson | `/courses/manage/<course_id>/lessons/<lesson_id>/edit/` |
| Edit quiz | `/quizzes/manage/<course_id>/` |

`CourseForm` accepts title, description and the lesson-order option. It has no
thumbnail/image field. `LessonForm` accepts title, plain-text content, optional
HTTP(S) video URL and optional file. There is no rich-text/Markdown editor,
video player or video-upload feature.

Attachments use Django filesystem storage at
`media/course_attachments/YYYY/MM/`, with a 10 MiB limit and extension
allowlist: `.pdf`, `.doc`, `.docx`, `.ppt`, `.pptx`, `.txt`, `.png`, `.jpg`,
`.jpeg`, `.zip`. Downloads are not exposed as public media URLs; the
permission-checked view streams an attachment with an allowlisted content
type. Validation checks size and filename extension, not malware or actual
file contents.

**Example (not pre-created):** create “Python Fundamentals”, add “Introduction
to Python”, “Variables and Data Types” and “Conditional Statements”, reorder
if needed, add quiz settings/questions/choices, preview, then publish. Learners
can then find and enrol in it. Courses, lessons, enrolments, quizzes and
results are database records (SQLite by default); attachments are local files
and videos remain external URLs. Compose mounts the database volume but not
`media/`, so uploaded attachments do not persist with that volume on container
recreation.

## 3. Technology and architecture

Versions below are from the checked local environment; requirements are ranges,
not exact pins.

| Technology | Use / trade-off |
|---|---|
| Python 3.12.13; Django 5.2.18 | Language and integrated web framework, ORM, auth, forms, templates and admin; ties application to Django's request/ORM conventions. |
| SQLite | Default relational database; easy local setup, less write concurrency than PostgreSQL. Quiz submission takes a SQLite write lock before counting attempts, but does not retry sustained contention. |
| DRF 3.18.3; drf-spectacular 0.30.0 | API serializers/auth/pagination/throttling and OpenAPI/Swagger UI at `/api/schema/`, `/api/docs/`; API does not author courses or take quizzes. |
| django-environ 0.14.0 | Reads environment and optional `.env`; keep real settings private. |
| Django templates + hand-written CSS | Server-rendered UI, ordinary form navigation, no frontend build; files under `templates/`, `static/css/`. |
| WhiteNoise 6.12.0; Gunicorn 26.2.0 | Serves collected static files and runs WSGI app in Docker; Gunicorn setup is not a TLS terminator or private-media service. |
| Ruff 0.17.1; coverage 7.16.2 | Lint/format and coverage tooling; coverage is not production-readiness. |
| Docker/Compose; GitHub Actions | Container example and push-triggered checks; Compose's fixed secret must be replaced before deployment. |
| gettext catalogs | English/Malayalam translation; Malayalam requires fluent review. |

**Request path:** browser/API client → `config/urls.py` and app URLs → view
checks role/ownership and validates input → shared service/access helper →
Django ORM/storage → rendered template, redirect, download or JSON response.
Web and API reuse `courses.services` and `courses.access`.

| Component | Responsibility |
|---|---|
| `config/` | Settings, root URLs, WSGI/ASGI; configures `accounts.User`. |
| `accounts/` | User roles, signup/login, home routing, permission mixins, user admin. `OwnerCourseMixin` also scopes course ownership. |
| `courses/` | Course/lesson/enrolment/progress/certificate, visibility, attachments, services and idempotent `seed_demo`. |
| `quizzes/` | Quiz/question/choice, authoring, grading and immutable attempts; depends on course/enrolment models. |
| `api/` | DRF JSON endpoints, reusing shared access and business logic. |
| `templates/`, `static/`, `locale/` | Server-rendered HTML, CSS and translation catalogs. |

## 4. Data model and design choices

A foreign key stores a relationship between rows; unique constraints prevent
duplicate relationships even when requests race. Progress percentages are
derived from completion rows, not separately stored.

| Model | Relationship / constraints and reason |
|---|---|
| `accounts.User` | Django `AbstractUser` plus `role` (`instructor`/`learner`); signup defaults to learner. App checks roles in views/mixins, not a database role constraint. |
| `courses.Course` | Instructor FK, title/description/status (`draft`/`published`), `enforce_order`, timestamps. Instructor deletion uses `PROTECT`, preserving owned courses. |
| `courses.Lesson` | Course FK, title/plain-text content, optional URL/file and position. Course deletion cascades. `(course, position)` is unique; services maintain ordering. |
| `courses.Enrollment` | User/course FKs and enrolment timestamp; unique `(user, course)` prevents duplicate membership and anchors progress/attempts. |
| `courses.LessonProgress` | Enrollment/lesson FKs and completion time; unique `(enrollment, lesson)` prevents duplicate completion. `clean()` checks same-course membership, but it is Django validation—not a DB constraint—and `save()` does not call it automatically. |
| `courses.Certificate` | One-to-one with enrollment, random unique UUID and issue time. Printed holder/course names come from current related records, not frozen snapshots. |
| `quizzes.Quiz` | One-to-one course relation; pass mark (default 70), nullable max attempts (`NULL` = unlimited), `show_answers`. |
| `quizzes.Question` | Quiz FK, text and position; ordered within quiz. |
| `quizzes.Choice` | Question FK, text and correctness. Conditional unique constraint allows at most one correct choice; editor formset requires exactly one and 2–6 nonblank choices. Direct privileged database/admin edits may leave zero correct choices. |
| `quizzes.QuizAttempt` | Enrollment/quiz FKs, stored score/total/percent/pass status/time. A new row per submission preserves history and avoids recalculating saved scores after pass-mark changes. |
| `quizzes.AttemptAnswer` | Attempt/question FKs, nullable selected-choice FK; unique `(attempt, question)`. Question uses `PROTECT`; deleted selected choices use `SET_NULL`, preserving attempt/result. |

For example, a learner's enrolment links them to a course; each lesson links
to that course and has an order; a completion row links the enrolment to one
lesson; an attempt stores the quiz outcome and answer rows. Deleting a lesson
also deletes its progress rows, so derived progress can change with the
course's current lesson count. Attempts retain score/pass outcome, but not a
full snapshot of question/choice text; later result review uses current quiz
questions.

**Engineering choices and trade-offs:**

- Separate apps/services keep identity, course, quiz and API logic findable and
  let web/API share rules; this adds module boundaries and cross-app imports.
- Role field plus server-side gates is simple for two roles; more granular
  roles may need Django groups/permissions.
- Drafts return 404 to non-owners; owners may preview. Publishing requires a
  lesson. Enrolled users keep course/lesson access after unpublish, but quizzes
  and attachments currently require publication.
- Ordered moves use transactional services and unique `(course, position)`;
  Up/Down is simpler than drag-and-drop but does not support branching curricula.
- Progress rows avoid synchronization of a stored percentage, but progress
  changes if lessons are added/deleted. Snapshots would be stable but need
  synchronization policy.
- Certificate eligibility is all lessons complete plus a pass if a populated
  quiz exists; one-to-one issuance is POST-only and UUID-verifiable. Current
  names/course title are not a historical snapshot.
- Forms, constraints, validators and grading enforce server-side input rules;
  they do not replace malware scanning, URL policy or deployment controls.
- Catalogue supports substring title/description search, instructor filter,
  stable sorting and 9-item pagination; it is not full-text search. Annotations
  and query-count tests avoid one query per listed course.

## 5. Permissions and security

| Control | Implementation and limits |
|---|---|
| Authentication/passwords | Django auth and `UserCreationForm`, with four configured password validators. No email verification or custom password-reset workflow. |
| Roles/ownership | `InstructorRequiredMixin`: anonymous → login redirect, learner → 403. `OwnerCourseMixin` scopes lookups to current instructor; another instructor's course is 404. Role is not DB-enforced; protect admin access. |
| Draft visibility | Shared course visibility helper gives 404 to non-owner/non-enrollee. A published course without enrolment gives 403 for learner-only actions such as quiz/attachment access. |
| CSRF/state changes | Django CSRF middleware; browser state changes use CSRF-protected POSTs (logout, enrol, completion, publish, certificates and language switch), not GET. DRF session-auth POSTs retain CSRF protection; token clients protect/send their token. |
| Lesson locking | Shared access rule protects lesson page, completion and attachment; API returns 409 for locked lessons. Application rule, not DB constraint: future write paths must call it. |
| File handling | 10 MiB/extension validation, permission/order checks and allowlisted response content type; `/media/` is not publicly routed. No virus scan or content-signature validation; local media/Compose persistence needs production work. |
| Output | Django autoescaping; lesson text uses `linebreaks`; script-escaping test exists; external links use `noopener noreferrer`. Avoid unsafe future `safe`/JavaScript usage. |
| API | Auth required by default; session and token auth, token at `/api/v1/auth/token/`; anonymous/user throttles are 30/120 per minute. No equivalent custom web-login throttle; throttling does not replace infrastructure controls. |

## 6. REST API

Routes are mounted at `/api/v1/`. Course collection/detail and enrolment
listings are read-only; there are no course-authoring, quiz or certificate API
operations.

| Method and route | Behavior |
|---|---|
| `POST /api/v1/auth/token/` | Exchange username/password for `{"token": "..."}`. |
| `GET /api/v1/courses/` | Published courses, 10 per page. |
| `GET /api/v1/courses/<course_id>/` | Course fields and ordered lesson `id`, `position`, `title`; published/owner/enrolled visibility. |
| `POST /api/v1/courses/<course_id>/enrol/` | Authenticated user enrols in published course; 201 new, 200 existing. Role is not restricted, so instructors can enrol too. No body required. |
| `GET /api/v1/my/enrolments/` | Caller-only enrolments with derived progress. |
| `POST /api/v1/courses/<course_id>/lessons/<lesson_id>/complete/` | Mark accessible lesson complete; 201 new, 200 already complete. No body required. |
| `GET /api/schema/`, `GET /api/docs/` | OpenAPI schema and Swagger UI. |

Errors: protected anonymous requests 401; non-enrolled learner action 403;
hidden draft/foreign lesson 404; locked lesson 409; unsupported method 405;
throttled request 429. Enrolment writes are idempotent, though status is
selected from a pre-write existence check. Tests cover these contracts.

Example (replace placeholders; never commit a real token):

```sh
curl -X POST http://127.0.0.1:8000/api/v1/auth/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"<username>","password":"<password>"}'
curl http://127.0.0.1:8000/api/v1/courses/ \
  -H 'Authorization: Token <token>'
curl -X POST http://127.0.0.1:8000/api/v1/courses/<course_id>/enrol/ \
  -H 'Authorization: Token <token>'
curl -X POST \
  http://127.0.0.1:8000/api/v1/courses/<course_id>/lessons/<lesson_id>/complete/ \
  -H 'Authorization: Token <token>'
```

Course-list JSON is paginated (`count`, `next`, `previous`, `results`); course
items include `id`, `title`, `instructor`, `lesson_count`, `created_at`.
Enrolment responses include `id`, nested course, `enrolled_at` and
`progress: {completed, total, percent}`. Completion responses include the
lesson, `created`, and updated progress.

## 7. Tests and code quality

Tests live in `accounts/tests.py`, `courses/tests.py`, `quizzes/tests.py`,
`api/tests.py` and `config/tests.py`. Coverage includes model constraints and
deletion; signup roles/safe redirects; ownership and draft rules; file limits
and access; lesson order; progress/certificates; quiz grading, attempt limits,
rollback/concurrency; API auth/pagination/throttling/statuses; admin behavior;
translations and selected accessibility behavior.

Verified in this checkout: **417 tests passed**, `manage.py check` reported no
issues, migration drift check found no changes, Ruff lint and format checks
passed. This run did not measure coverage; no local percentage is claimed.
CI runs tests under coverage, excluding tests/migrations in `.coveragerc`.
Passing tests do not prove production security, load capacity, full WCAG
conformance or operational readiness.

```sh
python manage.py test
python manage.py check
python manage.py makemigrations --check --dry-run
ruff format --check .
ruff check .
coverage run manage.py test && coverage report --show-missing
```

## 8. Local setup, Docker and deployment

macOS/local setup from a fresh checkout:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Generate a Django key with
`python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'`
and put it in `.env` as `SECRET_KEY=...`. Do not overwrite an existing `.env`
without preserving its settings. Then:

```sh
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

Open `http://127.0.0.1:8000/`. `seed_demo` idempotently creates an instructor,
two learners, a published five-lesson course with quiz/activity and a
two-lesson draft. It resets demo passwords and prints credentials; use only
locally, never expose demo accounts publicly. Credentials are in `README.md`.
`.env.example` enables `DEBUG=True` for local use; disable for deployment.
Create an admin with `python manage.py createsuperuser`; admin status alone
does not give the LMS instructor role. Set that role for trusted staff in
Django admin. If `SECRET_KEY` is missing/empty, startup intentionally fails;
add a local hostname to `ALLOWED_HOSTS` if rejected. Uploads must meet the
10 MiB/extension policy; learner 404s on drafts are intentional.

Docker details:

- `Dockerfile`: `python:3.12-slim`, install requirements, collect static assets,
  run non-root `app`; Gunicorn runs `config.wsgi:application` with 3 workers,
  WhiteNoise serves static files.
- `docker-entrypoint.sh`: migrations, optional `seed_demo` when
  `SEED_DEMO=1`, then supplied process.
- Compose maps port 8000 and persists SQLite at `/data/db.sqlite3` on
  `demo-data`; it seeds demo data but does not persist uploaded `media/`.
  `docker compose up --build` starts it.
- **Security caveat:** `docker-compose.yml` contains fixed placeholder
  `SECRET_KEY`; do not treat it as secret or expose this setup. Replace it with
  a strong runtime secret before sharing/deployment. Docker build uses a
  separate build-only key for `collectstatic`; runtime still requires a real
  key.
- Push CI installs requirements, generates a throwaway key, runs Ruff,
  migration check and tests under coverage. It does not deploy.

Production would need managed secrets, HTTPS/TLS, secure cookies and other
Django deployment settings, PostgreSQL for write concurrency, durable private
object storage, malware scanning, logging and backups. Current container is
not a verified production service.

## 9. Translation and accessibility

English is default; Malayalam is configured with `LocaleMiddleware`, the
language context processor and a CSRF-protected POST switcher retaining the
current path. Sources/compiled catalogs are
`locale/ml/LC_MESSAGES/django.po/.mo`; the `.mo` is committed, so gettext is
not needed at runtime. Refresh with `makemessages -l ml` and
`compilemessages -l ml`. Translations were filled in one pass and need fluent
review.

Accessibility work includes a focusable skip-link target, visible keyboard
focus, labelled fields/errors, live messages, landmarks, labelled progress,
quiz fieldsets/legends and names for repeated actions. Tests cover selected
behaviors, not full WCAG conformance or assistive-technology testing.

## 10. Repository map and feature entry points

```text
accounts/       User, roles, auth, mixins and tests
courses/        Courses, lessons, enrolment, progress, certificates, services
  management/commands/seed_demo.py
quizzes/        Quiz/questions/choices/attempts, editor, grader and tests
api/            DRF serializers, views, routes and tests
config/         Settings, root URLs, WSGI/ASGI, admin/settings tests
templates/      Shared, account, course, quiz and partial templates
static/css/     style.css, app.css, certificate.css
locale/ml/      Malayalam .po/.mo
.github/workflows/ci.yml; Dockerfile; docker-compose.yml; docker-entrypoint.sh
requirements.txt; .env.example; ruff.toml; .coveragerc; manage.py
```

| Feature | Start here |
|---|---|
| Course create/publish | `courses/forms.py`, `views.py`, `services.py`, `urls.py` |
| Lesson order/access | `courses/models.py`, `forms.py`, `views.py`, `services.py`, `access.py` |
| Quiz editor/taking/grading | `quizzes/forms.py`, `views.py`, `services.py`, `models.py` |
| Roles/auth | `accounts/models.py`, `forms.py`, `views.py`, `mixins.py` |
| Progress/certificates | `courses/services.py`, `models.py`, `views.py` |
| API | `api/urls.py`, `views.py`, `serializers.py`, `config/settings.py` |
| Tests | `accounts/tests.py`, `courses/tests.py`, `quizzes/tests.py`, `api/tests.py`, `config/tests.py` |
| Settings/deployment | `config/settings.py`, `config/urls.py`, `.env.example`, Docker files, CI |

## 11. Demo plan (about 6½ minutes)

Use local seeded data only; never expose demo credentials publicly.

| Time | Show / explain | Code/tests |
|---|---|---|
| 0:00–0:40 | Role routing and instructor landing dashboard. | `accounts/views.py`, `accounts/tests.py` |
| 0:40–1:40 | Manage course, status, lesson order, quiz link; explain text, video and private file. Use disposable DB for edits. | `courses/forms.py`, `views.py`, tests |
| 1:40–2:20 | Quiz pass mark, attempt limit, choice formsets; preview saves no attempt. | `quizzes/forms.py`, `views.py`, tests |
| 2:20–3:00 | Publish, switch to learner, search/filter, show draft invisibility and enrolment. | `courses/views.py`, `access.py`, tests |
| 3:00–4:00 | Complete lesson; show derived progress and a locked later lesson if ordered. | `courses/services.py`, tests |
| 4:00–4:45 | Submit quiz; show server-graded result/history/review and retained attempts. | `quizzes/services.py`, tests |
| 4:45–5:25 | Instructor analytics/CSV, issue/print certificate and verify UUID. | `courses/views.py`, services, tests |
| 5:25–6:10 | `/api/docs/`; explain learner API and lack of authoring/quiz endpoints. | `api/urls.py`, tests |
| 6:10–6:30 | SQLite/local-media caveats and verified checks. | Docker, CI, tests |

## 12. Interview quick answers

- **What does it solve?** Internal structured staff training with learner
  completion/assessment tracking.
- **Why separate apps/custom user?** They separate identity, courses, quizzes
  and API. Custom role belongs on `User` from the initial migration so all
  foreign keys target the configured user model.
- **Why Enrollment and progress rows?** Enrollment uniquely anchors course
  membership, progress and attempts; one completion row per lesson prevents
  duplicates, while derived percentages cannot drift from source rows.
- **How is ordering safe?** Unique `(course, position)` plus transactional
  reorder services first move positions above the current range, then assign
  final values.
- **How are permissions enforced?** Mixins gate roles; owner-scoped querysets
  return 404 for another instructor's course; drafts are 404 to non-owners;
  grading checks choice/question ownership server-side.
- **What happens on unpublish?** Hide from catalogue and block new enrolment;
  existing learners retain course/lesson data, but quiz/attachment flows
  currently require publication.
- **What does the API support?** Session/token authentication; published
  course read, enrolment and completion. It does not author courses or handle
  quizzes/certificates.
- **What are the main limits?** SQLite contention, local uploads without
  scanning/persistent Compose volume, no full question-text attempt snapshot,
  dynamic certificate names, unreviewed Malayalam, no email workflows, and
  selected (not full) accessibility checks.
- **Is passing tests production readiness?** No; this checkout's 417 tests
  passed, but tests do not prove production security, load or operations.
- **What would you improve?** Replace Compose placeholder secret; deploy with
  PostgreSQL, private object storage, scanning, HTTPS/security settings,
  logging/backups; review translations and add notifications/categories or
  versioned quiz/certificate snapshots as needed.

## 13. Limitations and future work

Not implemented: notifications, email verification/password reset/reminders,
course categories, bulk learner management, rich text/Markdown, thumbnails,
embedded video, quiz API and course-authoring API. Attachment checks do not
scan malware or verify file signatures; storage is local. Compose doesn't
persist media and has a fixed secret. HTTPS redirects, HSTS and secure-cookie
flags are not configured. SQLite can contend on writes. Quiz attempts store
scores/results, but reviews use current question/choice text. Certificates
display current learner/course values rather than frozen snapshots.
Translations need fluent review; accessibility has not had a full audit.

The prior security review fixed only the first four approved findings; other
items remain listed in `NOTES.md`. These are documented follow-ups, not claims
that every local/demo use is broken.

## 14. Quick reference

- **Stack:** Python 3.12, Django 5.2, SQLite, DRF, templates/CSS,
  WhiteNoise/Gunicorn in Docker.
- **Apps/models:** `accounts`, `courses`, `quizzes`, `api`; `User`, `Course`,
  `Lesson`, `Enrollment`, `LessonProgress`, `Certificate`, `Quiz`, `Question`,
  `Choice`, `QuizAttempt`, `AttemptAnswer`.
- **Pages:** login `/accounts/login/`, signup `/accounts/signup/`, catalogue
  `/courses/`, learner courses `/courses/my/`, instructor management
  `/courses/manage/`, API docs `/api/docs/`, admin `/admin/`.
- **Authoring:** instructor course page → add lesson / add-edit quiz → publish
  after at least one lesson.
- **Commands:** `migrate`, `seed_demo`, `runserver`, `test`; lint with
  `ruff format --check . && ruff check .`.
- **Core design:** derived progress; server-side/shared access checks; every
  quiz attempt stored separately.
- **Remember:** demo data is local; drafts are hidden; quiz grading is
  server-side; locked lessons cannot be bypassed by URL/POST; Docker setup is
  not production-hardened.
