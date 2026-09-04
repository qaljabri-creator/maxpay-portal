"""The merchant report's filters (spec §8).

The shared base minus the merchant dimension, which is the point: a merchant's
report is already one merchant's, so a control offering to filter by merchant
would either be a no-op or a way to ask about somebody else's.
"""

from apps.finance.report_forms import ReportFilterFormBase


class MerchantReportFilterForm(ReportFilterFormBase):
    """Dates, status, type and payment method. No merchant, no free text."""
