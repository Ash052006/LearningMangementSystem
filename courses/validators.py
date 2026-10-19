"""File validators for lesson attachments.

Kept out of models.py so forms and services can reuse them.
"""

from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.files.base import File
from django.utils.translation import gettext_lazy as _

MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB

ALLOWED_VIDEO_URL_SCHEMES = frozenset({"http", "https"})

# Allowlisted download types, keyed by extension.  A downloaded attachment is
# served with the type from this map rather than a type guessed from the file,
# so a stored file can never be delivered as, say, text/html.  The allowed
# extensions are derived from it so the two lists cannot drift apart.
ATTACHMENT_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".zip": "application/zip",
}

ALLOWED_ATTACHMENT_EXTENSIONS = frozenset(ATTACHMENT_CONTENT_TYPES)


def attachment_content_type(filename: str) -> str | None:
    """Return the allowlisted MIME type for ``filename``, or ``None``.

    ``None`` means the extension is not allowlisted, so the caller must refuse
    to serve the file rather than guess a type for it.
    """
    return ATTACHMENT_CONTENT_TYPES.get(Path(filename).suffix.lower())


def validate_video_url(value: str) -> None:
    """Allow only http/https video links.

    Django's ``URLField`` also accepts ftp/ftps by default; lesson videos
    should be ordinary web links, so the lesson form adds this validator.
    """
    if not value:
        return
    if urlsplit(value).scheme.lower() not in ALLOWED_VIDEO_URL_SCHEMES:
        raise ValidationError(
            _("Enter a video URL starting with http:// or https://."),
            code="video_url_scheme_not_allowed",
        )


def validate_attachment_size(file: File) -> None:
    """Reject files larger than MAX_ATTACHMENT_SIZE."""
    size = getattr(file, "size", None)
    if size is not None and size > MAX_ATTACHMENT_SIZE:
        raise ValidationError(
            _("Attachment must be %(max_mb).0f MB or smaller."),
            code="attachment_too_large",
            params={"max_mb": MAX_ATTACHMENT_SIZE / (1024 * 1024)},
        )


def validate_attachment_extension(file: File) -> None:
    """Reject files whose extension is not in ALLOWED_ATTACHMENT_EXTENSIONS."""
    suffix = Path(file.name).suffix.lower()
    if suffix not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise ValidationError(
            _("Unsupported file type '%(ext)s'. Allowed: %(allowed)s."),
            code="attachment_extension_not_allowed",
            params={
                "ext": suffix or "-",
                "allowed": ", ".join(sorted(ALLOWED_ATTACHMENT_EXTENSIONS)),
            },
        )
