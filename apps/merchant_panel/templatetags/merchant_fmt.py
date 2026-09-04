"""Rendering helpers for a panel whose context holds no model instances.

The merchant screens are handed serializer output — plain JSON-safe values — so
that no template can walk an object back to the client (see
:mod:`apps.merchant_panel.views`). The price of that is timestamps arriving as
ISO strings, which Django's ``date`` filter silently renders as an empty string.

``|when`` is the missing half: parse the ISO string back into an aware datetime
and format it in the project's timezone. One filter is a small price for a
context that cannot leak.
"""

from django import template
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.formats import date_format

register = template.Library()


@register.filter
def when(value, fmt="Y-m-d H:i"):
    """Format an ISO 8601 string as local time, or return "" if it is not one."""
    if not value:
        return ""
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is None:
            return ""
    else:
        parsed = value
    if timezone.is_aware(parsed):
        parsed = timezone.localtime(parsed)
    return date_format(parsed, fmt)
