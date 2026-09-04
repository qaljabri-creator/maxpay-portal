"""Where a withdrawal is paid to (spec §5, §6) — build-order step 10.

A withdrawal carries one field a deposit does not: ``destination_account``, the
client's own card or wallet number. It is the single most dangerous string in
the system. Spec §2 hands it to the merchant — they cannot pay without it — and
once a merchant has transferred against it, a digit the client mistyped is money
gone to a stranger. There is no undo and no reconciliation that finds it.

So this module does the only two things that actually help:

* **Normalise.** A number is typed on an Arabic keyboard, pasted out of a
  banking app, or read off a card in groups of four. ``٠٧٧٠ ١٢٣-٤٥٦٧`` and
  ``07701234567`` are the same account, and storing them as two different
  strings would make the merchant's job harder, not the client's easier. What
  is stored is digits and nothing else.
* **Refuse what cannot be an account.** Letters, punctuation that is not a
  separator, a length outside the configured band. This is a typo guard, not a
  validation: no checksum exists that covers every Iraqi rail, and a number that
  passes here can still be the wrong person's.

The guard that catches what neither can is in the screen, not in the code: the
client is shown the normalised digits back, grouped, before they submit. That is
the last point at which a wrong number is still free to fix.

Nothing here is method-specific. ``PaymentMethod`` carries no format, and
inventing one per rail would be a rule this codebase cannot keep true as Finance
adds methods — a wrong format refuses a legitimate client, which is worse than
a loose one that the confirmation step covers.
"""

import unicodedata

from django.conf import settings
from django.utils.translation import gettext_lazy as _

#: Characters a human puts *between* the digits of an account number: spaces of
#: every width, the separators cards and IBANs are printed with, and the bidi
#: marks an RTL paragraph wraps a Latin-digit run in. All meaningless, all
#: dropped rather than treated as a malformed number.
SEPARATORS = frozenset(
    " \t-–—_.،,/\\|()[]"
    "    "   # non-breaking and thin spaces
    "‎‏؜"          # LRM, RLM, ALM
    "⁦⁧⁨⁩"    # the isolate marks
    "٬"                       # Arabic thousands separator
)


class DestinationError(Exception):
    """A destination that must not be stored, with a message fit for a client."""

    def __init__(self, code: str, message):
        super().__init__(code)
        self.code = code
        self.message = message


def digit_bounds() -> tuple[int, int]:
    """The configured length band, counted in digits."""
    minimum = int(getattr(settings, "PORTAL_DESTINATION_MIN_DIGITS", 6))
    maximum = int(getattr(settings, "PORTAL_DESTINATION_MAX_DIGITS", 32))
    return minimum, maximum


def normalise(raw) -> str:
    """Reduce what the client typed to digits, or raise :class:`DestinationError`.

    Arabic-Indic and Eastern Arabic-Indic digits become their ASCII equivalents,
    separators are dropped, and anything else stops the whole string: a letter in
    an account number is not noise to be swallowed, it is a sign the client typed
    something other than the number.
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise DestinationError(
            "destination_missing", _("أدخل رقم البطاقة أو المحفظة التي ستستلم المبلغ.")
        )

    digits = []
    for char in text:
        if char in SEPARATORS:
            continue
        value = unicodedata.digit(char, None)
        # Covers ٠-٩ (U+0660) and ۰-۹ (U+06F0) alongside ASCII.
        if value is None:
            raise DestinationError(
                "destination_invalid",
                _("رقم الوجهة يقبل الأرقام فقط. راجع ما أدخلته."),
            )
        digits.append(str(value))

    number = "".join(digits)
    minimum, maximum = digit_bounds()
    if len(number) < minimum:
        raise DestinationError(
            "destination_too_short",
            _("رقم الوجهة أقصر مما ينبغي (%(min)s أرقام على الأقل).")
            % {"min": minimum},
        )
    if len(number) > maximum:
        raise DestinationError(
            "destination_too_long",
            _("رقم الوجهة أطول مما ينبغي (%(max)s رقمًا على الأكثر).")
            % {"max": maximum},
        )
    return number


def grouped(number: str, size: int = 4) -> str:
    """The stored digits in reading groups, for showing a client back their own
    number: ``07701234567`` → ``0770 1234 567``.

    Presentation only. What is stored, compared and handed to the merchant is
    always the unbroken run of digits.
    """
    return " ".join(number[index : index + size] for index in range(0, len(number), size))
