"""Small shared EN/PL/FR catalog for the three operator interfaces.

English templates are message IDs. Machine events and broadcast data stay in
their original form; translation happens only when a dashboard renders them.
An unknown language falls back to English, as does an unknown message.
"""

SUPPORTED_LANGUAGES = ("en", "pl", "fr")

# Each entry supplies Polish and French in that order. Keeping the two beside
# the English source makes omissions and mismatched placeholders reviewable.
_MESSAGES: dict[str, tuple[str, str]] = {
    "Quit": ("Zakończ", "Quitter"),
    "Rescan": ("Skanuj ponownie", "Relancer le balayage"),
    "Pause view": ("Wstrzymaj widok", "Suspendre l’affichage"),
    "Resume view": ("Wznów widok", "Reprendre l’affichage"),
    "Consensus": ("Konsensus", "Consensus"),
    "Stations": ("Stacje", "Stations"),
    "Log": ("Dziennik", "Journal"),
    "Waiting for the first completed cycle…": (
        "Oczekiwanie na pierwszy ukończony cykl…",
        "En attente du premier cycle terminé…",
    ),
    "TIME UNAVAILABLE": ("CZAS NIEDOSTĘPNY", "HEURE INDISPONIBLE"),
    "The receiver did not stop within {timeout:g} s; killed.": (
        "Odbiornik nie zatrzymał się w ciągu {timeout:g} s; proces zakończono wymuszeniem.",
        "Le récepteur ne s’est pas arrêté en {timeout:g} s ; processus tué.",
    ),
    "The receiver is not ready for a rescan.": (
        "Odbiornik nie jest gotowy do ponownego skanowania.",
        "Le récepteur n’est pas prêt pour un nouveau balayage.",
    ),
    "Rescan requested; it starts after the current cycle.": (
        "Zlecono ponowne skanowanie; rozpocznie się po bieżącym cyklu.",
        "Balayage demandé ; il commencera après le cycle en cours.",
    ),
    "VIEW PAUSED · the receiver keeps running": (
        "WIDOK WSTRZYMANY · odbiornik nadal pracuje",
        "AFFICHAGE SUSPENDU · le récepteur continue de fonctionner",
    ),
    "VIEW PAUSED · the receiver keeps running · p resumes": (
        "WIDOK WSTRZYMANY · odbiornik nadal pracuje · p wznawia widok",
        "AFFICHAGE SUSPENDU · le récepteur continue de fonctionner · p pour reprendre",
    ),
    "error: cannot open a window: {error}": (
        "błąd: nie można otworzyć okna: {error}",
        "erreur : impossible d’ouvrir une fenêtre : {error}",
    ),
    "Connecting…": ("Łączenie…", "Connexion…"),
    "No stations yet: the acquisition scan chooses them.": (
        "Brak stacji: zostaną wybrane podczas skanowania.",
        "Aucune station pour le moment : le balayage initial les sélectionnera.",
    ),
    "This dashboard needs JavaScript to follow the receiver.": (
        "Ten panel wymaga JavaScript do wyświetlania danych odbiornika.",
        "Ce tableau de bord nécessite JavaScript pour suivre le récepteur.",
    ),
    "Connection lost; reconnecting…": (
        "Utracono połączenie; ponowne łączenie…",
        "Connexion perdue ; reconnexion…",
    ),
    "The rescan request failed:": (
        "Nie udało się zlecić skanowania:",
        "La demande de balayage a échoué :",
    ),
    "Not found.": ("Nie znaleziono.", "Introuvable."),
    "Unknown host name.": ("Nieznana nazwa hosta.", "Nom d’hôte inconnu."),
    "The X-Requested-By header is missing.": (
        "Brakuje nagłówka X-Requested-By.",
        "L’en-tête X-Requested-By est absent.",
    ),
    "Requests from other origins are not accepted.": (
        "Żądania z innych źródeł nie są akceptowane.",
        "Les requêtes provenant d’autres origines sont refusées.",
    ),
    "Rescans are accepted only from this machine.": (
        "Ponowne skanowanie można zlecić tylko z tego komputera.",
        "Les demandes de balayage ne sont acceptées que depuis cette machine.",
    ),
    "error: cannot listen on {host}:{port}: {error}": (
        "błąd: nie można nasłuchiwać na {host}:{port}: {error}",
        "erreur : impossible d’écouter sur {host}:{port} : {error}",
    ),
    "Dashboard at {url} (Ctrl+C stops it and the receiver)": (
        "Panel pod adresem {url} (Ctrl+C zatrzymuje panel i odbiornik)",
        "Tableau de bord sur {url} (Ctrl+C arrête le tableau de bord et le récepteur)",
    ),
    "warning: the dashboard has no authentication; anyone who can reach this address can watch it. Rescans are accepted only from this machine.": (
        "uwaga: panel nie wymaga uwierzytelnienia; każdy z dostępem do tego adresu może go oglądać. Ponowne skanowanie można zlecić tylko z tego komputera.",
        "attention : le tableau de bord n’a pas d’authentification ; toute personne pouvant atteindre cette adresse peut le consulter. Les demandes de balayage ne sont acceptées que depuis cette machine.",
    ),
    "STARTING": ("URUCHAMIANIE", "DÉMARRAGE"),
    "ACQUISITION": ("WYSZUKIWANIE", "RECHERCHE"),
    "MAINTENANCE": ("MONITOROWANIE", "SUIVI"),
    "STOPPED": ("ZATRZYMANO", "ARRÊTÉ"),
    "HIGH": ("WYSOKIE", "ÉLEVÉE"),
    "MEDIUM": ("ŚREDNIE", "MOYENNE"),
    "LOW": ("NISKIE", "FAIBLE"),
    "STALE": ("NIEAKTUALNE", "PÉRIMÉE"),
    "OUTLIER": ("ODSTAJĄCA", "ABERRANTE"),
    "ok": ("OK", "OK"),
    "not counted": ("nieuwzględniona", "non retenue"),
    "no CT": ("brak CT", "sans CT"),
    "none": ("brak", "aucun"),
    "MHz": ("MHz", "MHz"),
    "PS": ("PS", "PS"),
    "PI": ("PI", "PI"),
    "RSSI dB": ("RSSI dB", "RSSI dB"),
    "Groups": ("Grupy", "Groupes"),
    "Corr.": ("Kor.", "Corr."),
    "Last CT": ("Ostatni CT", "Dernier CT"),
    "LTO": ("LTO", "LTO"),
    "Age": ("Wiek", "Âge"),
    "Trust": ("Zaufanie", "Confiance"),
    "Status": ("Stan", "État"),
    "± is source dispersion and holdover, not a calibrated UTC error.": (
        "± opisuje rozrzut źródeł i podtrzymanie czasu, a nie skalibrowany błąd względem UTC.",
        "± décrit la dispersion des sources et le maintien de l’heure, pas une erreur UTC étalonnée.",
    ),
    "Ignored a line that is not JSON.": (
        "Pominięto wiersz, który nie jest poprawnym JSON-em.",
        "Ligne ignorée : ce n’est pas du JSON valide.",
    ),
    "Ignored a JSON value that is not an event.": (
        "Pominięto wartość JSON, która nie jest zdarzeniem.",
        "Valeur JSON ignorée : ce n’est pas un événement.",
    ),
    "Event schema {schema} is not {supported}; showing what this version understands.": (
        "Schemat zdarzenia {schema} różni się od {supported}; wyświetlane są obsługiwane dane.",
        "Le schéma {schema} diffère de {supported} ; seules les données reconnues sont affichées.",
    ),
    "Ignored a malformed {kind} event.": (
        "Pominięto nieprawidłowe zdarzenie {kind}.",
        "Événement {kind} mal formé ignoré.",
    ),
    "Receiver ready: tuner type {tuner}, {gains} gain steps.": (
        "Odbiornik gotowy: typ tunera {tuner}, liczba poziomów wzmocnienia: {gains}.",
        "Récepteur prêt : tuner de type {tuner}, {gains} niveaux de gain.",
    ),
    "Acquisition #{iteration}: checking {listed} listed stations, {start:.2f}-{end:.2f} MHz.": (
        "Wyszukiwanie #{iteration}: sprawdzanie stacji z listy ({listed}), {start:.2f}-{end:.2f} MHz.",
        "Recherche n°{iteration} : vérification des stations de la liste ({listed}), {start:.2f}-{end:.2f} MHz.",
    ),
    "Acquisition #{iteration}: one WIDE capture at {center:.2f} MHz covers {start:.2f}-{end:.2f} MHz.": (
        "Wyszukiwanie #{iteration}: jeden zapis WIDE przy {center:.2f} MHz obejmuje {start:.2f}-{end:.2f} MHz.",
        "Recherche n°{iteration} : une capture WIDE à {center:.2f} MHz couvre {start:.2f}-{end:.2f} MHz.",
    ),
    "Acquisition #{iteration}: scanning {start:.2f}-{end:.2f} MHz in {step:.2f} MHz steps.": (
        "Wyszukiwanie #{iteration}: skanowanie {start:.2f}-{end:.2f} MHz z krokiem {step:.2f} MHz.",
        "Recherche n°{iteration} : balayage de {start:.2f} à {end:.2f} MHz par pas de {step:.2f} MHz.",
    ),
    "  {freq:6.2f} MHz  {db} dB  {groups} groups{ct}  {ps}": (
        "  {freq:6.2f} MHz  {db} dB  liczba grup: {groups}{ct}  {ps}",
        "  {freq:6.2f} MHz  {db} dB  groupes : {groups}{ct}  {ps}",
    ),
    "Watchlist empty: no RDS station found in this scan.": (
        "Lista obserwowanych stacji jest pusta: skanowanie nie wykryło stacji RDS.",
        "Liste de suivi vide : aucune station RDS détectée lors de ce balayage.",
    ),
    "Watchlist: {freqs} MHz.": (
        "Obserwowane stacje: {freqs} MHz.",
        "Stations suivies : {freqs} MHz.",
    ),
    "WIDE capture at {center:.2f} MHz, {rate:.2f} MS/s: {n_stations} stations at once.": (
        "Zapis WIDE przy {center:.2f} MHz, {rate:.2f} MS/s: liczba stacji odbieranych jednocześnie: {n_stations}.",
        "Capture WIDE à {center:.2f} MHz, {rate:.2f} Méch/s : stations reçues simultanément : {n_stations}.",
    ),
    "{n_groups} groups, {corrected} corrected": (
        "liczba grup: {n_groups}, po korekcji: {corrected}",
        "groupes : {n_groups}, après correction : {corrected}",
    ),
    "{label}: no CT, {groups}.": ("{label}: brak CT, {groups}.", "{label} : sans CT, {groups}."),
    "{label}: CT {utc:%H:%M}Z (LTO {lto}), {groups}.": (
        "{label}: CT {utc:%H:%M}Z (LTO {lto}), {groups}.",
        "{label} : CT {utc:%H:%M}Z (LTO {lto}), {groups}.",
    ),
    "Cycle #{iteration}: {observations} new CT observation(s); trust {trust}, N={sources}.": (
        "Cykl #{iteration}: nowe obserwacje CT: {observations}; zaufanie {trust}, N={sources}.",
        "Cycle n°{iteration} : nouvelles observations CT : {observations} ; confiance {trust}, N={sources}.",
    ),
    "iteration limit reached": ("osiągnięto limit iteracji", "limite d’itérations atteinte"),
    "stopped on request": ("zatrzymano na żądanie", "arrêt demandé"),
    "Receiver stopped: {reason}.": (
        "Odbiornik zatrzymany: {reason}.",
        "Récepteur arrêté : {reason}.",
    ),
    "The receiver stream ended without recon_stopped.": (
        "Strumień odbiornika zakończył się bez zdarzenia recon_stopped.",
        "Le flux du récepteur s’est terminé sans événement recon_stopped.",
    ),
    "Receiver process exited with status {returncode}.": (
        "Proces odbiornika zakończył się z kodem {returncode}.",
        "Le processus du récepteur s’est terminé avec le code {returncode}.",
    ),
    "Could not start the receiver: {error}": (
        "Nie udało się uruchomić odbiornika: {error}",
        "Impossible de démarrer le récepteur : {error}",
    ),
    " · {completed}/{total} points": (
        " · punkty: {completed}/{total}",
        " · {completed}/{total} points",
    ),
    " · {completed}/{total} stations": (
        " · stacje: {completed}/{total}",
        " · {completed}/{total} stations",
    ),
    " · next rescan in {remaining:.0f} s": (
        " · ponowne skanowanie za {remaining:.0f} s",
        " · prochain balayage dans {remaining:.0f} s",
    ),
    " · no stations to visit": (" · brak stacji do odbioru", " · aucune station à recevoir"),
    " · hop {current}/{total}": (" · przeskok {current}/{total}", " · saut {current}/{total}"),
    " · recording {freq:.2f} MHz": (
        " · odbiór {freq:.2f} MHz",
        " · acquisition à {freq:.2f} MHz",
    ),
    " · no stations to record": (" · brak stacji do odbioru", " · aucune station à recevoir"),
    " · recording {stations} stations at once": (
        " · jednoczesny odbiór stacji: {stations}",
        " · acquisition simultanée de {stations} stations",
    ),
    " · {stations} stations from one capture": (
        " · stacje z jednego zapisu: {stations}",
        " · {stations} stations issues d’une capture",
    ),
    "assessed {elapsed:.0f} s ago": (
        "oceniono {elapsed:.0f} s temu",
        "évaluation il y a {elapsed:.0f} s",
    ),
    "the last estimate expired: its CT is {age:.0f} s old": (
        "ostatnie oszacowanie wygasło: jego CT ma {age:.0f} s",
        "la dernière estimation a expiré : son CT date de {age:.0f} s",
    ),
    "Outliers: {freqs} MHz": ("Stacje odstające: {freqs} MHz", "Stations aberrantes : {freqs} MHz"),
    "age {age:.0f} s": ("wiek {age:.0f} s", "âge {age:.0f} s"),
    "Host clock minus consensus: {delta:+.1f} s": (
        "Zegar komputera minus konsensus: {delta:+.1f} s",
        "Horloge de l’hôte moins consensus : {delta:+.1f} s",
    ),
    "no active sources": ("brak aktywnych źródeł", "aucune source active"),
    "single-source": ("jedno źródło", "source unique"),
    "outlier-only": ("tylko dane odstające", "uniquement des valeurs aberrantes"),
    "error: {flag} applies to live reception, not --from-dir": (
        "błąd: {flag} dotyczy odbioru na żywo, nie --from-dir",
        "erreur : {flag} nécessite une réception en direct, sans --from-dir",
    ),
    "error: the dashboard requires the optional 'textual' dependency; install with: pip install 'rdsclock[tui]'": (
        "błąd: panel wymaga opcjonalnej biblioteki 'textual'; zainstaluj: pip install 'rdsclock[tui]'",
        "erreur : le tableau de bord nécessite la dépendance facultative 'textual' ; installer avec : pip install 'rdsclock[tui]'",
    ),
    "error: the desktop window requires tkinter ({error}); install your Python's Tk support (for example: apt install python3-tk)": (
        "błąd: okno aplikacji wymaga tkinter ({error}); zainstaluj obsługę Tk dla Pythona (np. apt install python3-tk)",
        "erreur : la fenêtre nécessite tkinter ({error}) ; installer la prise en charge de Tk pour Python (par exemple : apt install python3-tk)",
    ),
}

TRANSLATIONS = {
    language: {message: pair[index] for message, pair in _MESSAGES.items()}
    for index, language in enumerate(SUPPORTED_LANGUAGES[1:])
}


def normalize_language(language: str | None) -> str:
    """Accept a language or locale tag, using English for unsupported values."""
    code = (language or "en").strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in SUPPORTED_LANGUAGES else "en"


def translate(language: str | None, message: str, **values: object) -> str:
    """Translate a presentation template, then interpolate its named values.

    Values are never translated or interpreted as templates. Unknown messages
    without interpolation values remain verbatim, including literal braces.
    """
    template = TRANSLATIONS.get(normalize_language(language), {}).get(message, message)
    return template.format(**values) if values else template
