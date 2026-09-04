"""Upload validation (spec §11: extension and MIME validated, size capped)."""

import mimetypes
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _


@deconstructible
class FileSizeValidator:
    """Reject uploads larger than ``MAX_UPLOAD_SIZE_BYTES``."""

    def __init__(self, max_bytes: int | None = None):
        self.max_bytes = max_bytes

    @property
    def limit(self) -> int:
        return self.max_bytes or settings.MAX_UPLOAD_SIZE_BYTES

    def __call__(self, value):
        size = getattr(value, "size", None)
        if size is None:
            return
        if size > self.limit:
            raise ValidationError(
                _("حجم الملف %(size)s ميغابايت يتجاوز الحد المسموح %(limit)s ميغابايت."),
                code="file_too_large",
                params={
                    "size": round(size / (1024 * 1024), 2),
                    "limit": round(self.limit / (1024 * 1024), 2),
                },
            )

    def __eq__(self, other):
        return isinstance(other, FileSizeValidator) and self.max_bytes == other.max_bytes


@deconstructible
class UploadContentValidator:
    """Check the extension and the guessed content type against the allow-lists.

    This is a first gate only. It reads the *declared* type, which a client
    controls, so the upload path also inspects the file's real signature before
    it is stored.
    """

    def __init__(self, extensions: list[str] | None = None, content_types: list[str] | None = None):
        self.extensions = extensions
        self.content_types = content_types

    @property
    def allowed_extensions(self) -> list[str]:
        return [e.lower() for e in (self.extensions or settings.ALLOWED_UPLOAD_EXTENSIONS)]

    @property
    def allowed_content_types(self) -> list[str]:
        return [c.lower() for c in (self.content_types or settings.ALLOWED_UPLOAD_CONTENT_TYPES)]

    def __call__(self, value):
        name = getattr(value, "name", "") or ""
        extension = Path(name).suffix.lower().lstrip(".")
        if extension not in self.allowed_extensions:
            raise ValidationError(
                _("امتداد الملف «%(ext)s» غير مسموح. المسموح: %(allowed)s."),
                code="bad_extension",
                params={"ext": extension or "?", "allowed": ", ".join(self.allowed_extensions)},
            )

        declared = getattr(getattr(value, "file", None), "content_type", None)
        content_type = (declared or mimetypes.guess_type(name)[0] or "").lower()
        if content_type and content_type not in self.allowed_content_types:
            raise ValidationError(
                _("نوع الملف «%(ctype)s» غير مسموح."),
                code="bad_content_type",
                params={"ctype": content_type},
            )

    def __eq__(self, other):
        return (
            isinstance(other, UploadContentValidator)
            and self.extensions == other.extensions
            and self.content_types == other.content_types
        )


validate_upload_size = FileSizeValidator()
validate_upload_content = UploadContentValidator()


# ---------------------------------------------------------------------------
# What the file actually is
# ---------------------------------------------------------------------------

#: Leading bytes that identify the formats spec §11 allows. WebP is checked
#: separately because its marker is split across two ranges.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
]

#: Enough for every signature above, and small enough to read from any file.
_SNIFF_BYTES = 16


def sniff_content_type(file) -> str | None:
    """The type ``file`` really is, read from its own bytes, or ``None``.

    Independent of the name and of the ``Content-Type`` the browser declared,
    both of which the uploader controls. The read position is restored, so the
    caller can still save the file afterwards.
    """
    if file is None:
        return None
    try:
        position = file.tell()
    except (AttributeError, OSError):
        position = None
    try:
        file.seek(0)
        head = file.read(_SNIFF_BYTES)
    except (AttributeError, OSError):
        return None
    finally:
        try:
            file.seek(position or 0)
        except (AttributeError, OSError):
            pass

    if not head:
        return None
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            return content_type
    # RIFF....WEBP — four bytes of length sit between the two markers.
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_real_content_type(file) -> str:
    """Return what ``file`` really is, refusing anything not on the allow-list.

    :class:`UploadContentValidator` reads the declared type and is a first gate
    only; this is the one that decides. A PNG renamed to ``receipt.pdf`` fails
    here, and so does anything whose bytes match nothing we accept.
    """
    real = sniff_content_type(file)
    if real is None:
        raise ValidationError(
            _("تعذّر التعرف على نوع الملف. أرفق صورة أو ملف PDF."),
            code="unrecognised_content",
        )
    allowed = [c.lower() for c in settings.ALLOWED_UPLOAD_CONTENT_TYPES]
    if real not in allowed:
        raise ValidationError(
            _("نوع الملف «%(ctype)s» غير مسموح.") % {"ctype": real},
            code="bad_content_type",
        )

    # A name that disagrees with the bytes is either a mistake or an attempt to
    # get the file served as something it is not. Either way it is refused.
    declared = (mimetypes.guess_type(getattr(file, "name", "") or "")[0] or "").lower()
    equivalent = {"image/jpg": "image/jpeg"}
    declared = equivalent.get(declared, declared)
    if declared and declared != real:
        raise ValidationError(
            _("امتداد الملف لا يطابق محتواه."),
            code="extension_mismatch",
        )
    return real
