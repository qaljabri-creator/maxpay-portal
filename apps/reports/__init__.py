"""Reporting and export (spec §9) — build-order step 16.

A plain package rather than a Django app: it has no models, no migrations, no
templates and no signals, so registering it in ``INSTALLED_APPS`` would buy
nothing and imply state it does not have. What it holds is the half of
reporting that both panels share — the filter, the aggregation and the
workbook writer — with each panel keeping its own scoping, its own columns and
its own permission.

The split matters more here than it usually would. A report is the one feature
where the same query serves two audiences with opposite entitlements, and spec
§2 is unforgiving about the difference: Finance may see who the client was, a
merchant may never, and an exported file is exactly the artefact that outlives
the screen it left. So the shared code deliberately knows **nothing** about
which surface is calling it, and the merchant's rows arrive already masked by
the serializers that mask every other merchant-facing byte.
"""
