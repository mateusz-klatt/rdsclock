"""Run with python -I against the installed wheel, never an editable checkout."""

import importlib.metadata
import sys
from importlib import resources

import rdsclock
from rdsclock.i18n import SUPPORTED_LANGUAGES, translate
from rdsclock.web import WebDashboard

expected = sys.argv[1]
assert rdsclock.__version__ == importlib.metadata.version("rdsclock") == expected
assert SUPPORTED_LANGUAGES == ("en", "pl", "fr")
for asset in ("index.html", "app.js", "app.css"):
    assert resources.files("rdsclock").joinpath("webui", asset).read_bytes()
for language in SUPPORTED_LANGUAGES:
    dashboard = WebDashboard(["unused"], port=0, language=language)
    try:
        assert f'lang="{language}"' in dashboard.pages["/"].decode()
        assert dashboard.snapshot("127.0.0.1")[0]["columns"][4] == translate(language, "Groups")
    finally:
        dashboard.close()
print(f"Installed {expected}: EN/PL/FR and web assets verified ({rdsclock.__file__}).")
