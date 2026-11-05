"""Translation completeness, safe interpolation and presentation-only locales."""

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from string import Formatter
from types import ModuleType

import pytest

from rdsclock import cli
from rdsclock.dashboard import COLUMNS, UNCERTAINTY_NOTE, WHOLE_HOUR_HINT, DashboardState
from rdsclock.i18n import SUPPORTED_LANGUAGES, TRANSLATIONS, normalize_language, translate
from tests.fake_receiver import HOST_UTC, MARKUP_PS, ct_hop, ct_session, cycle, line, session


def _placeholders(message):
    return Counter(
        (field, spec, conversion)
        for _, field, spec, conversion in Formatter().parse(message)
        if field is not None
    )


@pytest.mark.parametrize(
    "language,expected",
    [(None, "en"), ("", "en"), ("EN", "en"), (" pl-PL ", "pl"), ("fr_FR", "fr"), ("de", "en")],
)
def test_language_tags_are_normalized_with_english_fallback(language, expected):
    assert normalize_language(language) == expected


def test_all_catalogs_have_the_same_messages_and_format_fields():
    assert tuple(TRANSLATIONS) == SUPPORTED_LANGUAGES[1:]
    keys = set(TRANSLATIONS["pl"])
    assert set(COLUMNS) | {UNCERTAINTY_NOTE} <= keys
    for catalog in TRANSLATIONS.values():
        assert set(catalog) == keys
        for source, target in catalog.items():
            assert target.strip(), source
            assert _placeholders(target) == _placeholders(source), source


def test_unknown_text_and_broadcast_values_are_not_translated_or_reformatted():
    assert translate("pl", "Station {literal}") == "Station {literal}"
    assert translate("fr", "Custom {value}", value="{untrusted}") == "Custom {untrusted}"
    assert translate("de", "Watchlist: {freqs} MHz.", freqs="98.80") == "Watchlist: 98.80 MHz."
    assert translate("pl", "{label}: no CT, {groups}.", label="STARTING {PS}", groups="2") == (
        "STARTING {PS}: brak CT, 2."
    )


@pytest.mark.parametrize(
    "language,ready,phase,age,offset,outliers,no_ct",
    [
        (
            "pl",
            "Odbiornik gotowy",
            "MONITOROWANIE",
            "wiek 41 s",
            "Zegar komputera",
            "Stacje odstające",
            "brak",
        ),
        (
            "fr",
            "Récepteur prêt",
            "SUIVI",
            "âge 41 s",
            "Horloge de l’hôte",
            "Stations aberrantes",
            "aucun",
        ),
    ],
)
def test_localized_session_preserves_broadcast_fields_and_machine_codes(
    language, ready, phase, age, offset, outliers, no_ct
):
    records = session()
    original = list(records)
    state = DashboardState(language=language, clock=lambda: 100.0, wall_clock=lambda: HOST_UTC)
    messages = [message for record in records for message in state.apply_line(record)]
    view = state.view()
    assert messages[0].startswith(ready)
    assert view.status.startswith(phase)
    assert view.consensus.age == age
    assert view.consensus.host_offset.startswith(offset)
    assert view.consensus.outliers.startswith(outliers)
    assert view.rows[1].cells[6] == no_ct
    assert view.phase == "MAINTENANCE"
    assert view.consensus.trust_level == "HIGH"
    assert [row.status for row in view.rows] == ["ok", "ok", "OUTLIER"]
    assert view.rows[0].cells[:3] == ("102.40", "STACJA A", "0xA101")
    assert view.rows[0].cells[6:8] == ("12:00Z", "+01:00")
    assert view.rows[2].cells[1] == MARKUP_PS
    assert records == original  # presentation never rewrites the JSONL stream
    assert set(view.as_dict()) == {"phase", "status", "consensus", "rows"}
    assert state.consensus == _english_consensus(records)


def _english_consensus(records):
    state = DashboardState()
    for record in records:
        state.apply_line(record)
    return state.consensus


@pytest.mark.parametrize(
    "language,notes,expired,malformed,stopped",
    [
        (
            "pl",
            "brak aktywnych źródeł",
            "ostatnie oszacowanie wygasło",
            "Pominięto",
            "Odbiornik zatrzymany",
        ),
        (
            "fr",
            "aucune source active",
            "la dernière estimation a expiré",
            "Événement",
            "Récepteur arrêté",
        ),
    ],
)
def test_localized_absence_expiry_errors_and_stop(language, notes, expired, malformed, stopped):
    now = [100.0]
    state = DashboardState(language=language, clock=lambda: now[0])
    state.apply_line(line(cycle(utc=None)))
    assert state.view().consensus.notes == notes
    for record in session():
        state.apply_line(record)
    now[0] += 301
    assert state.view().consensus.notes.startswith(expired)
    assert state.view().consensus.trust_level == "STALE"
    assert state.apply_line('{"schema": 1, "event": "hop_result"}')[0].startswith(malformed)
    record = {"schema": 1, "event": "recon_stopped", "reason": "iterations"}
    assert state.apply_line(json.dumps(record))[0].startswith(stopped)
    assert state.phase == "STOPPED"


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
@pytest.mark.parametrize(
    "flag,module,function",
    [("--tui", "tui", "run_dashboard"), ("--web", "web", "run_web"), ("--gui", "gui", "run_gui")],
)
def test_cli_passes_locale_to_every_frontend_but_not_receiver(
    monkeypatch, language, flag, module, function
):
    calls = []
    frontend = ModuleType(f"rdsclock.{module}")

    def run(command, *args, **kwargs):
        calls.append((command, kwargs))
        return 0

    setattr(frontend, function, run)
    monkeypatch.setitem(sys.modules, frontend.__name__, frontend)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    assert cli.main(["recon", flag, "--lang", language]) == 0
    command, kwargs = calls[0]
    assert kwargs == {"language": language}
    assert command[:5] == [sys.executable, "-m", "rdsclock", "recon", "--jsonl"]
    assert not any(arg.startswith("--lang") for arg in command)


def test_cli_language_is_optional_and_rejects_unsupported_values():
    assert cli.build_parser().parse_args(["recon", "--web"]).lang == "en"
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["recon", "--lang", "de"])
    assert error.value.code == 2


@pytest.mark.parametrize("flag", ["--tui", "--web", "--gui"])
def test_locale_also_applies_to_dashboard_start_errors(flag, capsys):
    assert cli.main(["recon", flag, "--lang", "pl", "--from-dir", "unused"]) == 2
    assert capsys.readouterr().err == f"błąd: {flag} dotyczy odbioru na żywo, nie --from-dir\n"


CT_READING_MESSAGES = (
    WHOLE_HOUR_HINT,
    "ambiguous",
    "nonexistent",
    "{utc:%H:%M}Z ← {zone}",
    "{local:%H:%M} {zone}: {status}, not used",
    "{label}: CT {local:%H:%M} read as {zone} local time = {utc:%H:%M}Z (LTO {lto}), {groups}.",
    "{label}: CT {local:%H:%M} as {zone} local time is {status}, not used (LTO {lto}), {groups}.",
    "CT read as local time by --ct-timezone: {rules}.",
)


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES[1:])
def test_ct_reading_and_whole_hour_hint_are_fully_translated(language):
    from rdsclock.rds_clock import ClockTime
    from tests.fake_receiver import STATION_A

    assert set(CT_READING_MESSAGES) <= set(TRANSLATIONS[language])
    skipped = line(ct_hop(STATION_A, ClockTime(datetime(2026, 3, 29, 2, 30, tzinfo=UTC), 0)))
    state = DashboardState(language=language, clock=lambda: 100.0, wall_clock=lambda: HOST_UTC)
    shown = [message for record in ct_session() for message in state.apply_line(record)]
    shown += [cell for row in state.view().rows for cell in row.cells]
    shown += state.apply_line(skipped) + list(state.view().rows[0].cells)
    # Without rules, a host clock an hour behind the consensus brings the hint.
    hinted = DashboardState(
        language=language,
        clock=lambda: 100.0,
        wall_clock=lambda: HOST_UTC - timedelta(hours=1),
    )
    shown += [message for record in session() for message in hinted.apply_line(record)]
    text = "\n".join(shown)
    assert "--ct-timezone" in text and "Europe/Warsaw" in text
    for english in ("read as", "local time", "not used", "ambiguous", "nonexistent", "hint"):
        assert english not in text, english
