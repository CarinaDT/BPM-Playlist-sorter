# BPM-Playlist-Tool (Prototyp)

Erstellt Spotify-Playlists nach BPM. Spotify liefert seit November 2024 für neue Apps keine BPM mehr,
deshalb holt das Tool sie von GetSongBPM, speichert sie in einer SQLite-Datenbank und baut daraus
neue Playlists.

Nur Python 3 (Standardbibliothek), es muss nichts installiert werden.

## Ablauf

    Spotify-Playlist  ->  SQLite (tracks)  ->  GetSongBPM (BPM)  ->  SQL-Abfrage nach BPM  ->  neue Playlist

## Einrichtung

1. **Spotify**: Du brauchst ein Premium-Konto. Auf https://developer.spotify.com/dashboard eine App
   anlegen, "Web API" auswählen und als Redirect URI genau `http://127.0.0.1:8888/callback`
   eintragen (nicht `localhost`). Die Client ID kopieren.
2. **GetSongBPM**: Auf https://getsongbpm.com/api einen API-Key beantragen. Die Seite verlangt einen
   Link zurück auf GetSongBPM.com und bittet bei privaten oder Lernprojekten um eine kurze Nachricht.
3. Die Datei `.env.example` nach `.env` kopieren und beide Werte eintragen.

## Benutzung

    python3 bpm_playlist.py login                       # einmalig, öffnet den Browser
    python3 bpm_playlist.py playlists                   # deine Playlists mit IDs
    python3 bpm_playlist.py import <ID oder Link>       # Songs in die Datenbank laden
    python3 bpm_playlist.py lookup --limit 5            # BPM holen (erst nur 5 zum Testen)
    python3 bpm_playlist.py stats                       # wie viele Songs haben BPM?
    python3 bpm_playlist.py make --min 120 --max 130 --dry-run
    python3 bpm_playlist.py make --min 120 --max 130 --name "Laufen 125"

Optionen bei `make`: `--from-playlist <ID>` (nur Songs aus einer Playlist), `--half-double`
(60 BPM zählt auch für 120, und umgekehrt), `--limit N`.

## Was geprüft ist und was nicht

**Geprüft** (`python3 test_bpm_playlist.py`, 6 Tests): Die komplette Logik läuft gegen einen lokalen
Fake-Server, der Spotify und GetSongBPM so nachbildet, wie die Dokumentation sie beschreibt:
Login mit PKCE inklusive Token-Erneuerung, Playlist-Import über mehrere Seiten, Matching von Titel
und Künstler (Remaster-Zusätze, falscher Künstler, ähnlicher Titel), Cache, Wiederholung nach
"zu viele Anfragen", BPM-Auswahl per SQL, Halb-/Doppeltempo, Playlist anlegen, Songs in
100er-Paketen hinzufügen. Dass die Tests Fehler erkennen, habe ich mit drei absichtlich eingebauten
Fehlern geprüft.

**Nicht geprüft**, weil meine Umgebung keine externen Dienste erreicht und ich deine Zugangsdaten
nicht habe: ob die echten Server genau so antworten wie in der Doku. Das sind die Stellen, die du
zuerst testen solltest:

1. **Songs zu einer Playlist hinzufügen** (`POST /playlists/{id}/items`). Der Migrationsguide von
   Spotify nennt nur die Umbenennung von `/tracks` zu `/items`. Dass der Body weiterhin `uris`
   heißt, habe ich nicht bestätigt gefunden. Im Spotify-Forum berichten Entwickler außerdem von
   403-Fehlern beim Hinzufügen im Entwicklermodus, die Ursache habe ich nicht klären können. Teste
   das deshalb früh mit einem kleinen echten `make` (`--limit 3`).
2. **Antwortformat von GetSongBPM** bei "kein Treffer" und bei der Suche `type=both`. Der Code
   behandelt alles, was keine Liste ist, als "kein Treffer".
3. **Trefferquote**: Wie viele deiner Songs GetSongBPM kennt, weiß niemand vorher. `stats` zeigt es
   dir nach `lookup`. Das ist die wichtigste Zahl für die Frage, ob das Tool für dich taugt.
4. **Genauigkeit der BPM**: GetSongBPM-Werte können falsch sein, oft um den Faktor 2. Prüf ein paar
   Songs, die du kennst.

Die Stelle, an der es am ehesten hakt, ist also ein einzelner Schritt, nicht das Gesamtkonzept.

## Wie du es nachbauen kannst (Reihenfolge)

1. Konfiguration und die HTTP-Hilfsfunktion `http_json` (Zeile für Zeile verstehen, alles baut darauf auf).
2. Datenbank: `SCHEMA`, `connect`, `store_track`.
3. BPM-Teil mit dem Matching: `clean_title`, `normalize`, `pick_best`, `fetch_candidates`, `run_lookup`.
   Das ist der Data-Science-Kern, hier lohnt sich eigenes Experimentieren (Schwellenwert 0.85,
   Halb-/Doppeltempo, Trefferquote).
4. Auswahl per SQL: `select_tracks`.
5. Spotify-Login (PKCE) zuletzt, das ist der kniffligste Teil.
6. Zum Schluss `make` mit `create_playlist` und `add_items`.

## Kurz erklärt

- **PKCE**: Ein Login-Verfahren ohne Geheimnis in der App. Das Tool erzeugt ein Zufallswort
  (`verifier`) und schickt nur dessen Hash (`challenge`) mit. Beim Eintauschen des Codes beweist es
  mit dem Zufallswort, dass es dieselbe Anfrage ist.
- **Cache**: Jede BPM-Abfrage landet in der Tabelle `bpm`. Spätere Läufe fragen nur neue Songs an.
  Das schont das Limit von GetSongBPM.
- **Matching**: Der Künstler muss exakt passen, der Titel nur ähnlich (nach dem Entfernen von
  Zusätzen wie "- 2011 Remaster"). Lieber ein Song ohne BPM als ein Song mit falscher BPM.

## Sicherheit

`.env`, `.spotify_token.json` und `bpm_cache.db` stehen in `.gitignore` und gehören nie auf GitHub.
