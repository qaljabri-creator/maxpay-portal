"""Finding one request among all of them (Finance review 4.2).

Finance searches for a request the way it was described to them on the phone,
and that description is one of four things: the reference, the client's email,
their account number, or *the amount*. The first three the queue already had.
The fourth is the one this module exists for, and it is the one Finance
actually uses — a client rings about "the hundred dollars from Tuesday" far
more often than they quote a reference back.

Two rules the search obeys, and neither is negotiable:

* **One box, not four.** A search screen that asks *which kind* of thing you
  are about to type is a screen that makes the operator do the parsing. The
  term is matched against everything it could plausibly be, and what it cannot
  plausibly be is skipped — ``MX-0042`` is never compared against an amount
  column, because it is not a number.
* **Identity stays behind its permission (spec §2).** Matching on a client's
  email is a way of *confirming* an email without ever displaying it, so the
  identity half of the query is added only for a user holding
  ``accounts.view_client_identity``. A Finance user who has had it withdrawn
  searches references and amounts, and gets nothing back that they could not
  already see.
"""

from decimal import Decimal, InvalidOperation

from django.db.models import Q

#: The widest figure worth comparing against a stored amount. ``amount_usd`` is
#: ``numeric(12, 2)``, so ten digits ahead of the point is its whole range;
#: anything longer is not a mistyped amount, it is a different kind of thing.
MAX_INTEGER_DIGITS = 10

#: Thousands separators an operator might paste in, Arabic-Indic included.
_SEPARATORS = str.maketrans({",": None, "٬": None, "،": None, " ": None, " ": None})


def as_amount(term: str) -> Decimal | None:
    """``term`` as a figure to search on, or ``None`` if it is not one.

    Returns ``None`` rather than raising for everything that is not an amount,
    because "not an amount" is the ordinary case here — most searches are
    references — and a caller that has to catch an exception per keystroke is a
    caller that will eventually stop.
    """
    cleaned = term.translate(_SEPARATORS).strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None

    # ``Decimal`` parses "nan", "inf" and "1e40" quite happily; a database
    # column does not, and comparing against one would be an error rather than
    # a miss.
    if not value.is_finite() or value < 0:
        return None
    exponent = value.as_tuple().exponent
    if exponent < -2:
        # Three decimal places is not a stored amount. Rounding it to find a
        # near miss would be the search answering a question nobody asked.
        return None
    # ``adjusted()`` rather than counting digits: ``Decimal("1e40")`` has one
    # digit and forty zeroes after it, and a digit count cheerfully waves it
    # through into a comparison the column cannot make.
    if value != 0 and value.adjusted() >= MAX_INTEGER_DIGITS:
        return None
    return value


def query(term: str, *, with_identity: bool) -> Q:
    """The ``Q`` a free-text search expands to.

    ``with_identity`` is the caller's ``view_client_identity`` answer, passed
    rather than looked up so this stays a pure function that a test can put a
    plain boolean into.
    """
    term = term.strip()
    terms = Q(public_ref__icontains=term)

    amount = as_amount(term)
    if amount is not None:
        # Both the figure the request carries *now* and the figure it was
        # submitted with. After a correction those differ, and the client
        # ringing up will quote the one they typed, not the one that arrived —
        # which is the whole reason the original is kept (Finance review 3.1).
        terms |= (
            Q(amount_usd=amount)
            | Q(submitted_amount_usd=amount)
            | Q(amount_iqd=amount)
            | Q(submitted_amount_iqd=amount)
        )

    if with_identity:
        terms |= (
            Q(client__display_name__icontains=term)
            | Q(client__email__icontains=term)
            | Q(client__account_number__icontains=term)
            | Q(client__b2core_id__iexact=term)
        )
    return terms
