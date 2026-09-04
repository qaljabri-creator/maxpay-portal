"""The one rule this whole app exists to keep: a merchant never learns who the
client is (spec §2).

Spec §11 words the mechanism precisely — *all merchant-facing serializers
explicitly whitelist fields* — and this module is what makes that more than an
intention. Three layers, each catching what the one before it could miss:

1. **Class definition time.** :func:`check_field_names` refuses to build a
   serializer that declares an identifying field, or that reads one through
   ``source=``. A leak introduced by editing a serializer is an ``ImportError``
   at start-up, not a payload in production.
2. **Serialisation time.** Every :class:`~apps.merchant_panel.serializers.
   MerchantSafeSerializer` walks what it just produced and raises rather than
   return it. This is what covers a ``SerializerMethodField`` — the one field
   type whose output no static check can predict.
3. **Response time.** The merchant API's base view walks the finished payload
   once more, so anything *not* produced by a guarded serializer — a
   hand-built dict, a paginator wrapper, an error body — is covered too.

Every layer fails closed. A leak becomes a 500, which is an incident; a leak
that renders is a breach, which is the thing spec §2 exists to prevent, and
between the two there is no contest.

The check is on **names**, not on values, and that is deliberate. Names are what
a serializer whitelist controls; values are what free text carries. The message
thread is free text a merchant is meant to read (spec §5), so no value-level
filter could run there without breaking the feature. What the tests add on top
is a value-level sweep of every structured response, which is where a name
check and a value check meet.
"""

from django.utils.translation import gettext_lazy as _


class AnonymityError(Exception):
    """A merchant-facing payload was about to carry client identity."""


#: Exact keys that must never appear in anything a merchant receives.
#: ``merchant_selected`` is here even though it names a merchant rather than a
#: client: which merchant the client originally chose is Finance's routing
#: business (spec §5), and spec §2's list of what a merchant sees does not
#: include it.
FORBIDDEN_KEYS = frozenset({
    "client",
    "client_id",
    "customer",
    "customer_id",
    "b2core_id",
    "sub",
    "account_number",
    "display_name",
    "full_name",
    "email",
    "phone",
    "sender_id",
    "uploaded_by_id",
    "user",
    "user_id",
    "created_by",
    "created_by_id",
    "actor",
    "actor_label",
    "ip",
    "last_login_ip",
    "merchant_selected",
    "merchant_selected_id",
    "is_internal_note",
    "notes",
})

#: Substrings that make a key forbidden whichever way it is spelled. A future
#: ``client_reference`` or ``customerEmail`` is caught without anyone having to
#: have predicted the name. Chosen to be unambiguous in this payload
#: vocabulary: ``destination_account`` and ``wallet_number`` — both of which a
#: merchant legitimately sees (spec §2) — match none of them.
FORBIDDEN_SUBSTRINGS = (
    "client",
    "customer",
    "b2core",
    "email",
    "phone",
    "account_number",
    "display_name",
    "full_name",
    "sender_id",
    "uploaded_by_id",
    "identity",
    "internal_note",
    "audit",
)

#: Model attributes a merchant serializer must not reach through ``source=``.
#: A field called ``reference`` whose source is ``client.account_number`` passes
#: a check on names alone, so the traversal path is checked segment by segment.
FORBIDDEN_SOURCES = FORBIDDEN_KEYS | {"masked_label"}


def _normalise(key) -> str:
    return str(key).strip().lower()


def is_forbidden_key(key) -> bool:
    """Whether ``key`` may appear in a merchant-facing payload."""
    name = _normalise(key)
    if name in FORBIDDEN_KEYS:
        return True
    return any(token in name for token in FORBIDDEN_SUBSTRINGS)


def forbidden_source_segments(source: str) -> list[str]:
    """The segments of a dotted ``source=`` path that lead to client identity."""
    if not source or source == "*":
        return []
    return [
        segment
        for segment in str(source).split(".")
        if _normalise(segment) in FORBIDDEN_SOURCES or is_forbidden_key(segment)
    ]


def check_field_names(serializer_name: str, fields) -> None:
    """Refuse a serializer whose whitelist would expose client identity.

    ``fields`` is a mapping of field name to DRF field, i.e. exactly what
    ``_declared_fields`` holds at class-definition time.
    """
    offences: list[str] = []
    for name, field in fields.items():
        if is_forbidden_key(name):
            offences.append(f"field {name!r}")
        source = getattr(field, "source", None)
        for segment in forbidden_source_segments(source):
            offences.append(f"field {name!r} reads {source!r} (segment {segment!r})")

    if offences:
        raise AnonymityError(
            f"{serializer_name} would break client anonymity (spec §2): "
            + "; ".join(sorted(offences))
            + ". A merchant-facing serializer whitelists fields explicitly and "
            "client identity is never among them."
        )


def assert_anonymous(payload, *, where: str = "payload", _path: str = "") -> None:
    """Walk a rendered payload and raise on the first identifying key.

    Recursive on purpose: a nested list of messages or attachments is exactly
    where an unguarded dict would hide.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{_path}.{key}" if _path else str(key)
            if is_forbidden_key(key):
                raise AnonymityError(
                    f"{where} carries client identity at {here!r} (spec §2). "
                    "Nothing a merchant receives may name the client."
                )
            assert_anonymous(value, where=where, _path=here)
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_anonymous(item, where=where, _path=f"{_path}[{index}]")


#: What a merchant is told the client is called, everywhere (spec §5).
CLIENT_LABEL = _("العميل")
