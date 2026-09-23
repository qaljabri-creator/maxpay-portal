"""``manage.py`` names its settings or refuses to run.

It used to fall back to ``config.settings.dev`` when nothing was set. On a server
whose ``.env`` forgot ``DJANGO_SETTINGS_MODULE``, that ran ``migrate`` against
the production database with ``DEBUG`` on and prod.py's refusals skipped — and
said nothing. Now the only fallback is one a developer has to ask for.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

import manage

MANAGE_PY = Path(settings.BASE_DIR) / "manage.py"


class SettingsModuleTests(SimpleTestCase):
    def test_a_named_module_is_used_as_named(self):
        self.assertEqual(
            manage.settings_module({"DJANGO_SETTINGS_MODULE": "config.settings.prod"}),
            "config.settings.prod",
        )

    def test_nothing_named_is_refused(self):
        with self.assertRaises(manage.SettingsNotNamed) as caught:
            manage.settings_module({})
        self.assertIn("DJANGO_SETTINGS_MODULE", str(caught.exception))

    def test_an_empty_value_is_nothing_named(self):
        with self.assertRaises(manage.SettingsNotNamed):
            manage.settings_module({"DJANGO_SETTINGS_MODULE": "  "})

    def test_the_local_flag_is_the_only_way_to_dev(self):
        for value in ("true", "1", "yes", "on", "TRUE"):
            with self.subTest(value=value):
                self.assertEqual(
                    manage.settings_module({manage.LOCAL_DEV_FLAG: value}),
                    "config.settings.dev",
                )

    def test_a_flag_that_is_not_yes_is_not_a_flag(self):
        for value in ("false", "0", "", "prod"):
            with self.subTest(value=value):
                with self.assertRaises(manage.SettingsNotNamed):
                    manage.settings_module({manage.LOCAL_DEV_FLAG: value})

    def test_a_named_module_wins_over_the_flag(self):
        self.assertEqual(
            manage.settings_module({
                "DJANGO_SETTINGS_MODULE": "config.settings.prod",
                manage.LOCAL_DEV_FLAG: "true",
            }),
            "config.settings.prod",
        )


class ScriptTests(SimpleTestCase):
    """The real file, run the way an operator runs it.

    Copied to a directory with no ``.env`` beside it, so a developer's own
    ``.env`` — which names dev — cannot answer for the server that forgot to.
    """

    def run_copy(self, **extra_env):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copy(MANAGE_PY, directory)
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"DJANGO_SETTINGS_MODULE", manage.LOCAL_DEV_FLAG}
            }
            env.update(extra_env)
            return subprocess.run(
                [sys.executable, str(Path(directory) / "manage.py"), "check"],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

    def test_it_refuses_with_a_reason_and_a_failing_exit_code(self):
        result = self.run_copy()
        self.assertEqual(result.returncode, 1)
        self.assertIn("DJANGO_SETTINGS_MODULE is not set", result.stderr)
        self.assertIn("config.settings.prod", result.stderr)
