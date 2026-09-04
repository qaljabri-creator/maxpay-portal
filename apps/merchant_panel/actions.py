"""Carrying out a merchant's move (spec §8).

One function, and it exists for one reason: marking a withdrawal paid is a
lifecycle move *and* a file, and those two must land together. Spec §6 says the
merchant pays the client and uploads proof of transfer; a status that says
"paid" with no proof behind it is the thing Finance would have to chase later,
so the outer transaction makes the pair atomic.

Everything else is delegated. The rules are
:mod:`apps.transactions.services` — the only writer of ``Request.status``, and
the place that re-reads and locks the row, checks the permission, checks the
request is actually routed to this merchant, and writes the audit entry.
"""

from django.db import transaction

from apps.core.choices import ActorRole
from apps.core.validators import validate_real_content_type
from apps.transactions.models import Attachment
from apps.transactions.services import apply_transition


@transaction.atomic
def execute(request_obj, action: str, *, actor, form, http_request=None):
    """Apply ``action`` and store whatever the form carried with it.

    Raises :class:`~apps.transactions.services.TransitionError` if the move is
    not the merchant's to make — in which case nothing is written, the upload
    included.
    """
    updated = apply_transition(
        request_obj,
        action,
        actor=actor,
        http_request=http_request,
        **form.transition_kwargs(),
    )

    proof = getattr(form, "proof", None)
    if proof is not None:
        _store_proof(updated, proof, actor=actor, form=form)
    return updated


def _store_proof(request_obj, upload, *, actor, form) -> Attachment:
    """File the merchant's proof of transfer against the request (spec §8)."""
    attachment = Attachment(
        request=request_obj,
        file=upload,
        # What the bytes say, not what the browser declared. The form already
        # checked; re-reading here keeps the stored type right even if a caller
        # ever arrives with a form that did not.
        content_type=getattr(form, "proof_content_type", None)
        or validate_real_content_type(upload),
        uploaded_by_role=ActorRole.MERCHANT,
        uploaded_by_id=getattr(actor, "pk", None),
    )
    # original_name and size_bytes are filled in by the model's own save().
    attachment.full_clean(exclude=["request", "original_name", "size_bytes"])
    attachment.save()
    return attachment
