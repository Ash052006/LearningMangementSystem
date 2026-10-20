# Project notes

## Tools and AI

The project uses Python 3.12, Django 5.2, SQLite, `django-environ`, and Ruff
0.17; dependencies are managed with `venv` and `pip`. The pages and forms are
server-rendered, with no JavaScript build step. The project also includes a
Django REST Framework API, Docker/Gunicorn deployment files, and GitHub Actions
CI. Gettext tools were used for the Malayalam catalogue.

An AI assistant was used for code and test review, reasoning about Django
attachment headers and private-file access, analysing SQLite quiz-submission
concurrency, and reviewing CSV formula-injection handling. It also helped draft
the notes structure and demo seed data. The draft does not record a reliable
breakdown of which individual lines or decisions were written independently
versus with AI assistance.

An AI assistant also helped refresh the existing Django templates and CSS
presentation. No backend behavior or course data was changed for that
interface work.

## Interface

The UI is rendered by Django templates and styled by `static/css/style.css`
and `static/css/app.css`; it does not use a JavaScript framework or external
component library. The shared navigation is role-aware and includes a language
selector. Catalogue, learner, instructor, quiz, certificate and error pages
share responsive layouts, form and status styling, and accessible focus
indicators. Course artwork is decorative inline SVG rather than uploaded or
stock imagery. Existing form submissions, routes and permission checks remain
the source of application behavior.

## Data model and design choices

- `accounts.User` extends Django's `AbstractUser` and is configured before the
  first migration so application foreign keys consistently use the custom user
  model. The role is a field rather than a database constraint; permissions are
  checked in views and mixins.
- A `Course` belongs to an instructor and has draft/published status.
  Instructor deletion is protected to avoid silently deleting owned courses.
  `Lesson` positions are unique within a course, and ordering changes are
  handled by services.
- `Enrollment` is unique per learner and course, providing the common link for
  progress and attempts. `LessonProgress` is unique per enrollment and lesson;
  completion and percentages are derived from these rows instead of storing a
  percentage that could become stale.
- A course has at most one `Quiz`. Questions have positions, and a conditional
  constraint permits at most one correct choice per question.
- `QuizAttempt` and `AttemptAnswer` preserve submitted assessment history.
  Scores and pass/fail results are recorded at submission, rather than
  recalculated from later quiz edits. Answered questions are protected from
  deletion; a deleted selected choice can be cleared without rewriting the
  attempt's recorded result.

## Assumptions

- Signup role selection is available only when
  `ALLOW_INSTRUCTOR_SELF_SIGNUP=True`. It is disabled by default; signups then
  become learners, and an administrator grants instructor access. Enabling
  self-selection is an opt-in suitable for a demo, not the default policy.
- Learners and other non-owners receive 404 for draft or otherwise unavailable
  courses, avoiding disclosure of their existence. An instructor can preview
  their own course, including a draft.
- Unpublishing removes a course from the catalogue and prevents new
  enrolments, but learners already enrolled retain access to the course and
  their history.
- Quiz attempts are not overwritten. Each submission is retained as an
  assessment record, including its stored result.

## Not included / what I would change with more time

Notifications and email workflows (including verification, password reset,
and learner reminders) are not implemented. The catalogue does include search,
instructor filtering, sorting, and pagination; course categories and bulk
learner management are not included.

For production, I would move from SQLite to PostgreSQL, use managed secrets and
HTTPS, and serve uploaded files through private object storage or an internal
web-server redirect with upload scanning. The Malayalam translations were
generated in one pass and should be reviewed by a fluent speaker. A later
security pass fixed only the first four findings in its review; the remaining
findings were left out of scope.

## Time spent

** 6hrs**
