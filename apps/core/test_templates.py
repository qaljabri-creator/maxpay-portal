"""Traps the templates set that a reviewer keeps having to catch by eye.

Each case here is a defect that actually shipped, more than once, and that no
other test could see — a template renders fine, the page returns 200, and the
mistake is only visible to someone reading the rendered Arabic page closely
enough to notice an English sentence in the middle of it.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


def template_files():
    """Every template in the project, app directories included.

    Walked from the settings rather than from a hardcoded path: a template in an
    app that nobody remembered to list is exactly the one that would carry the
    defect quietly.
    """
    roots = [Path(d) for engine in settings.TEMPLATES for d in engine["DIRS"]]
    # `APP_DIRS` templates, found by looking rather than by a list somebody has
    # to remember to extend — an app nobody listed is exactly the one that would
    # carry the defect quietly.
    roots += sorted((Path(settings.BASE_DIR) / "apps").glob("*/templates"))

    seen = set()
    for root in roots:
        for path in sorted(root.rglob("*.html")):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


#: ``{#`` that never meets its ``#}`` before the line ends.
_OPENS = re.compile(r"\{#")


def unterminated_comment_lines(text):
    """Line numbers where a ``{# … #}`` comment runs past the end of its line."""
    offenders = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in _OPENS.finditer(line):
            if "#}" not in line[match.end():]:
                offenders.append(number)
                break
    return offenders


class TemplateCommentTests(SimpleTestCase):
    """``{# … #}`` is a **single-line** comment in the Django template language.

    Give it a second line and the parser stops looking at the end of the first
    one: everything after that is template *content*, and it is rendered to the
    page. On this project that means an English sentence about serializers
    printed into an Arabic screen a client is looking at.

    It has happened four times. Three of them were still in the tree when this
    test was written — two in the merchant panel and one above the rounding row
    in the deposit quote. So the fix is not the three files; the fix is this
    test, which fails on the next one before anybody has to spot it.

    ``{% comment %}`` is the multi-line form and has no such trap.
    """

    def test_no_template_has_a_multi_line_hash_comment(self):
        offenders = []
        for path in template_files():
            text = open(path, encoding="utf-8").read()
            for line in unterminated_comment_lines(text):
                offenders.append(f"{path}:{line}")

        self.assertEqual(
            offenders,
            [],
            "A `{# … #}` comment that does not close on its own line renders its "
            "remaining lines to the page. Use `{% comment %} … {% endcomment %}`:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_scan_actually_reaches_the_templates(self):
        """A guard that silently walked an empty tree would pass forever."""
        found = list(template_files())

        self.assertGreater(len(found), 20, found)
        names = {path.name for path in found}
        self.assertIn("flow.html", names)
        self.assertIn("request_detail.html", names)

    # -- the detector itself, so the guard above cannot rot into a no-op ----

    def test_it_catches_a_comment_that_runs_on(self):
        self.assertEqual(
            unterminated_comment_lines("<p>ok</p>\n{# first line\n   second #}\n"),
            [2],
        )

    def test_it_allows_a_comment_that_closes_on_its_line(self):
        self.assertEqual(unterminated_comment_lines("{# tidy #}\n<p>ok</p>\n"), [])

    def test_it_allows_two_comments_on_one_line(self):
        self.assertEqual(unterminated_comment_lines("{# one #} {# two #}\n"), [])

    def test_it_catches_a_second_comment_that_runs_on(self):
        """The first one closing is not a licence for the second."""
        self.assertEqual(unterminated_comment_lines("{# closed #} {# open\n#}\n"), [1])

    def test_it_says_nothing_about_the_comment_tag(self):
        self.assertEqual(
            unterminated_comment_lines("{% comment %}\n  many\n  lines\n{% endcomment %}"),
            [],
        )


class HiddenAttributeTests(SimpleTestCase):
    """``hidden`` has to actually hide.

    The user agent's ``[hidden] { display: none }`` is beaten by any author rule
    that sets ``display`` — ``.quote__row { display: flex }`` is enough — so a
    script setting ``node.hidden = true`` on such an element hides nothing. That
    shipped: the commission row in the deposit quote was toggled correctly by
    ``flow.js``, asserted by the JavaScript tests, and still sat on screen
    reading ``0 د.ع``.

    It had been patched a component at a time, with a ``[hidden]`` companion
    beside each offending rule, which works right up until somebody adds a
    component and does not know to. ``system.css`` states it once for every
    surface instead, and this is what keeps it there.
    """

    SYSTEM_CSS = Path(settings.BASE_DIR) / "static" / "css" / "system.css"

    def test_the_shared_reset_makes_hidden_win(self):
        css = open(self.SYSTEM_CSS, encoding="utf-8").read()

        self.assertRegex(
            css,
            r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important",
            "system.css must carry a global `[hidden] { display: none !important }`. "
            "Without it, any component whose class sets `display` ignores the "
            "attribute and a hidden row stays on screen.",
        )

    def test_every_document_root_loads_the_sheet_that_carries_it(self):
        """The rule is only worth anything on pages that load system.css.

        Checked against whichever templates actually open an ``<html>`` — every
        other one inherits from one of them — so a new surface added tomorrow
        has to answer this too rather than being forgotten on a list.
        """
        roots = [
            path
            for path in template_files()
            if "<html" in open(path, encoding="utf-8").read()
        ]

        self.assertTrue(roots, "no document-root template found")
        for path in roots:
            with self.subTest(template=path.name):
                self.assertIn("system.css", open(path, encoding="utf-8").read())
