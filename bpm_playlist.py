#!/usr/bin/env python3
"""BPM-Playlist-Tool (Prototyp)

Ablauf:  login -> import (Playlist -> SQLite) -> lookup (BPM holen) -> make (neue Playlist)

Nur Python-Standardbibliothek, es muss nichts installiert werden.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import http.server
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER_AGENT = "bpm-playlist-tool/0.1"
SCOPES = ("playlist-read-private playlist-read-collaborative "
          "playlist-modify-private playlist-modify-public")


# --------------------------------------------------------------------------
# Konfiguration (alles wird erst beim Aufruf gelesen, damit Tests es umbiegen können)
# --------------------------------------------------------------------------
def load_dotenv(path: Path | None = None) -> None:
    """Liest KEY=VALUE-Zeilen aus .env in die Umgebungsvariablen (ohne Zusatzpaket)."""
    path = path or HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def cfg(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Es fehlt {name}. Trag es in die Datei .env ein (Vorlage: .env.example).")
    return value


def api_base() -> str:
    return cfg("SPOTIFY_API_BASE", "https://api.spotify.com/v1").rstrip("/")


def accounts_base() -> str:
    return cfg("SPOTIFY_ACCOUNTS_BASE", "https://accounts.spotify.com").rstrip("/")


def bpm_base() -> str:
    return cfg("GETSONGBPM_BASE", "https://api.getsongbpm.com").rstrip("/")


def redirect_port() -> int:
    return int(cfg("SPOTIFY_REDIRECT_PORT", "8888"))


def redirect_uri() -> str:
    # Spotify verlangt die IP-Schreibweise 127.0.0.1, nicht "localhost"
    return f"http://127.0.0.1:{redirect_port()}/callback"


def token_file() -> Path:
    return Path(cfg("BPM_TOKEN_FILE", str(HERE / ".spotify_token.json")))


def db_path() -> Path:
    return Path(cfg("BPM_DB", str(HERE / "bpm_cache.db")))


# --------------------------------------------------------------------------
# HTTP-Hilfsfunktion
# --------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} bei {url}: {body[:300]}")
        self.status, self.body, self.url = status, body, url


def http_json(method: str, url: str, headers: dict | None = None,
              json_body: dict | None = None, form: dict | None = None,
              retries: int = 3) -> dict:
    """Schickt eine Anfrage und gibt die JSON-Antwort als dict zurück."""
    all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    all_headers.update(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        all_headers["Content-Type"] = "application/x-www-form-urlencoded"

    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            if err.code == 429 and attempt < retries:  # zu viele Anfragen -> warten, nochmal
                try:
                    wait = int(err.headers.get("Retry-After", "2"))
                except ValueError:
                    wait = 2
                time.sleep(min(wait, 30))
                continue
            raise ApiError(err.code, body, url) from None
        except urllib.error.URLError as err:
            raise ApiError(0, str(err.reason), url) from None
    raise ApiError(429, "zu viele Wiederholungen", url)


# --------------------------------------------------------------------------
# Spotify-Login (Authorization Code mit PKCE, kein Client Secret nötig)
# --------------------------------------------------------------------------
def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    return verifier, pkce_challenge(verifier)


def _callback_handler(result: dict):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":  # z. B. favicon.ico
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            result["code"] = query.get("code", [None])[0]
            result["error"] = query.get("error", [None])[0]
            result["state"] = query.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>Login erledigt. Du kannst dieses Fenster schließen.</h3>"
                             .encode("utf-8"))

        def log_message(self, *args):  # keine Zugriffs-Logs im Terminal
            pass

    return Handler


def _default_on_url(url: str) -> None:
    print("Öffne diese Adresse im Browser und erlaube den Zugriff:\n" + url)
    webbrowser.open(url)


def save_token(token: dict, old: dict | None = None) -> dict:
    stored = dict(old or {})
    stored["access_token"] = token["access_token"]
    stored["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    if token.get("refresh_token"):
        stored["refresh_token"] = token["refresh_token"]
    path = token_file()
    path.write_text(json.dumps(stored), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return stored


def login(on_url=None, timeout: int = 180) -> dict:
    client_id = cfg("SPOTIFY_CLIENT_ID", required=True)
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri(),
        "scope": SCOPES, "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
    }
    url = accounts_base() + "/authorize?" + urllib.parse.urlencode(params)

    result: dict = {}
    server = http.server.HTTPServer(("127.0.0.1", redirect_port()), _callback_handler(result))
    server.timeout = 1
    (on_url or _default_on_url)(url)
    deadline = time.time() + timeout
    try:
        while "state" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "state" not in result:
        sys.exit("Zeit abgelaufen: Es kam keine Antwort von Spotify an.")
    if result.get("error"):
        sys.exit(f"Spotify meldet: {result['error']}")
    if result["state"] != state:
        sys.exit("Sicherheitsprüfung (state) fehlgeschlagen, bitte nochmal versuchen.")

    token = http_json("POST", accounts_base() + "/api/token", form={
        "grant_type": "authorization_code", "code": result["code"],
        "redirect_uri": redirect_uri(), "client_id": client_id, "code_verifier": verifier,
    })
    return save_token(token)


def get_access_token() -> str:
    path = token_file()
    if not path.exists():
        sys.exit("Noch nicht eingeloggt. Erst ausführen: python3 bpm_playlist.py login")
    stored = json.loads(path.read_text(encoding="utf-8"))
    if stored["expires_at"] - 60 > time.time():
        return stored["access_token"]
    refreshed = http_json("POST", accounts_base() + "/api/token", form={
        "grant_type": "refresh_token", "refresh_token": stored["refresh_token"],
        "client_id": cfg("SPOTIFY_CLIENT_ID", required=True),
    })
    return save_token(refreshed, old=stored)["access_token"]


# --------------------------------------------------------------------------
# Spotify-API (Stand der Doku: Februar-2026-Migrationsguide)
# --------------------------------------------------------------------------
def spotify(method: str, path: str, **kwargs) -> dict:
    url = path if path.startswith("http") else api_base() + path
    headers = {"Authorization": "Bearer " + get_access_token()}
    return http_json(method, url, headers=headers, **kwargs)


def list_playlists():
    url = "/me/playlists?limit=50"
    while url:
        page = spotify("GET", url)
        yield from page.get("items", [])
        url = page.get("next")


def playlist_tracks(playlist_id: str):
    """Liefert Songs einer eigenen Playlist. Liest 'item' (neu) oder 'track' (alt)."""
    url = f"/playlists/{playlist_id}/items?limit=50"
    while url:
        page = spotify("GET", url)
        for entry in page.get("items", []):
            track = entry.get("item") or entry.get("track")
            if not track or track.get("is_local") or not track.get("id"):
                continue
            if track.get("type") not in (None, "track"):  # Podcast-Folgen überspringen
                continue
            yield {
                "id": track["id"], "uri": track["uri"], "title": track["name"],
                "artists": [a["name"] for a in track.get("artists", [])],
            }
        url = page.get("next")


def create_playlist(name: str, description: str) -> dict:
    return spotify("POST", "/me/playlists",
                   json_body={"name": name, "description": description, "public": False})


def add_items(playlist_id: str, uris: list[str]) -> None:
    for start in range(0, len(uris), 100):  # maximal 100 Songs pro Anfrage
        spotify("POST", f"/playlists/{playlist_id}/items",
                json_body={"uris": uris[start:start + 100]})


# --------------------------------------------------------------------------
# Datenbank
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    spotify_id   TEXT PRIMARY KEY,
    uri          TEXT NOT NULL,
    title        TEXT NOT NULL,
    artist       TEXT NOT NULL,          -- erster Künstler, zur Anzeige
    artists_json TEXT NOT NULL           -- alle Künstler als JSON-Liste, fürs Matching
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id TEXT NOT NULL,
    spotify_id  TEXT NOT NULL,
    PRIMARY KEY (playlist_id, spotify_id)
);
CREATE TABLE IF NOT EXISTS bpm (
    spotify_id     TEXT PRIMARY KEY,
    status         TEXT NOT NULL,        -- ok | no_match | error
    bpm            INTEGER,
    key_of         TEXT,
    matched_title  TEXT,
    matched_artist TEXT,
    match_score    REAL,
    source         TEXT NOT NULL DEFAULT 'getsongbpm',
    fetched_at     TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_track(conn: sqlite3.Connection, playlist_id: str, track: dict) -> None:
    conn.execute("INSERT OR IGNORE INTO tracks VALUES (?,?,?,?,?)", (
        track["id"], track["uri"], track["title"],
        track["artists"][0] if track["artists"] else "",
        json.dumps(track["artists"], ensure_ascii=False)))
    conn.execute("INSERT OR IGNORE INTO playlist_tracks VALUES (?,?)", (playlist_id, track["id"]))


# --------------------------------------------------------------------------
# Matching: Spotify-Song <-> GetSongBPM-Eintrag
# --------------------------------------------------------------------------
_PAREN_NOISE = re.compile(
    r"\s*[(\[][^)\]]*\b(?:remaster\w*|mono\b|stereo\b|radio edit|single version|album version"
    r"|feat\b\.?|ft\b\.?|featuring\b)[^)\]]*[)\]]", re.I)
_DASH_NOISE = re.compile(
    r"\s+[-–]\s+(?:\d{4}\s+)?(?:remaster\w*(?:\s+\d{4})?|mono\b|stereo\b|radio edit"
    r"|single version|album version|feat\b\.?|ft\b\.?).*$", re.I)


def clean_title(title: str) -> str:
    """Entfernt Zusätze wie '- 2011 Remaster' oder '(feat. X)'."""
    return _DASH_NOISE.sub("", _PAREN_NOISE.sub("", title)).strip()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_artist(name: str) -> str:
    return re.sub(r"^the ", "", normalize(name))


def parse_tempo(value) -> int | None:
    try:
        tempo = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return tempo if 40 <= tempo <= 250 else None


def pick_best(candidates: list, title: str, artists: list[str], min_title: float = 0.85):
    """Wählt den besten Treffer. Künstler muss exakt passen, Titel ähnlich genug sein."""
    wanted_artists = {normalize_artist(a) for a in artists}
    best = None
    for cand in candidates:
        cand_artist = cand.get("artist")
        if isinstance(cand_artist, list):
            cand_artist = cand_artist[0] if cand_artist else {}
        cand_artist = (cand_artist or {}).get("name", "")
        if normalize_artist(cand_artist) not in wanted_artists:
            continue
        cand_title = cand.get("song_title") or cand.get("title") or ""
        score = difflib.SequenceMatcher(
            None, normalize(clean_title(title)), normalize(clean_title(cand_title))).ratio()
        tempo = parse_tempo(cand.get("tempo"))
        if score >= min_title and tempo and (best is None or score > best[0]):
            best = (score, cand, tempo, cand_artist, cand_title)
    return best


def fetch_candidates(title: str, artist: str) -> list:
    key = cfg("GETSONGBPM_API_KEY", required=True)
    lookup = f"song:{clean_title(title)} artist:{artist}"
    query = urllib.parse.urlencode({"type": "both", "lookup": lookup, "limit": 10})
    response = http_json("GET", f"{bpm_base()}/search/?{query}", headers={"X-API-KEY": key})
    found = response.get("search")
    return found if isinstance(found, list) else []  # bei "kein Treffer" kommt ein Fehlerobjekt


def run_lookup(conn: sqlite3.Connection, refresh: bool = False,
               limit: int | None = None, delay: float = 0.4) -> None:
    if refresh:
        sql = "SELECT * FROM tracks"
    else:
        sql = ("SELECT t.* FROM tracks t LEFT JOIN bpm b USING (spotify_id) "
               "WHERE b.spotify_id IS NULL OR b.status = 'error'")
    rows = conn.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    for number, row in enumerate(rows, 1):
        artists = json.loads(row["artists_json"])
        label = f"{row['artist']} – {row['title']}"
        try:
            best = pick_best(fetch_candidates(row["title"], row["artist"]), row["title"], artists)
            if best:
                score, cand, tempo, m_artist, m_title = best
                conn.execute("INSERT OR REPLACE INTO bpm VALUES (?,?,?,?,?,?,?,?,?)", (
                    row["spotify_id"], "ok", tempo, cand.get("key_of"), m_title, m_artist,
                    round(score, 3), "getsongbpm", now_iso()))
                print(f"[{number}/{len(rows)}] {label} -> {tempo} BPM")
            else:
                conn.execute("INSERT OR REPLACE INTO bpm VALUES (?,?,?,?,?,?,?,?,?)", (
                    row["spotify_id"], "no_match", None, None, None, None, None,
                    "getsongbpm", now_iso()))
                print(f"[{number}/{len(rows)}] {label} -> kein Treffer")
        except ApiError as err:
            conn.execute("INSERT OR REPLACE INTO bpm VALUES (?,?,?,?,?,?,?,?,?)", (
                row["spotify_id"], "error", None, None, None, None, None,
                "getsongbpm", now_iso()))
            print(f"[{number}/{len(rows)}] {label} -> Fehler: {err}")
            if err.status in (401, 403):
                conn.commit()
                sys.exit("GetSongBPM lehnt den API-Key ab (401/403). Key und Registrierung prüfen.")
        conn.commit()
        time.sleep(delay)


def select_tracks(conn: sqlite3.Connection, low: int, high: int, playlist: str | None = None,
                  half_double: bool = False, limit: int | None = None) -> list:
    cond = "b.bpm BETWEEN :lo AND :hi"
    if half_double:  # Quellen verwechseln oft halbes/doppeltes Tempo (60 <-> 120)
        cond += " OR b.bpm * 2 BETWEEN :lo AND :hi OR b.bpm / 2.0 BETWEEN :lo AND :hi"
    sql = ("SELECT t.uri, t.title, t.artist, b.bpm FROM tracks t "
           "JOIN bpm b USING (spotify_id) WHERE b.status = 'ok' AND (" + cond + ")")
    params: dict = {"lo": low, "hi": high}
    if playlist:
        sql += " AND t.spotify_id IN (SELECT spotify_id FROM playlist_tracks WHERE playlist_id = :pl)"
        params["pl"] = playlist
    sql += " ORDER BY b.bpm, t.artist"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------
# Kommandozeile
# --------------------------------------------------------------------------
def extract_playlist_id(text: str) -> str:
    match = re.search(r"playlist/([A-Za-z0-9]+)", text)
    return match.group(1) if match else text.strip()


def cmd_login(_args) -> None:
    print(f"Wichtig: Im Spotify Developer Dashboard muss diese Redirect URI eingetragen sein:\n"
          f"  {redirect_uri()}")
    login()
    print("Login gespeichert.")


def cmd_playlists(_args) -> None:
    for playlist in list_playlists():
        meta = playlist.get("items") or playlist.get("tracks") or {}
        print(f"{playlist['id']}  {playlist['name']}  ({meta.get('total', '?')} Songs)")


def cmd_import(args) -> None:
    conn = connect()
    for raw in args.playlists:
        playlist_id = extract_playlist_id(raw)
        count = 0
        for track in playlist_tracks(playlist_id):
            store_track(conn, playlist_id, track)
            count += 1
        conn.commit()
        print(f"Playlist {playlist_id}: {count} Songs gespeichert.")


def cmd_lookup(args) -> None:
    run_lookup(connect(), refresh=args.refresh, limit=args.limit)


def cmd_stats(_args) -> None:
    conn = connect()
    rows = conn.execute("SELECT COALESCE(b.status, 'offen') AS status, COUNT(*) AS n "
                        "FROM tracks t LEFT JOIN bpm b USING (spotify_id) GROUP BY status").fetchall()
    total = sum(r["n"] for r in rows)
    for r in rows:
        print(f"{r['status']:10} {r['n']:5}  ({100 * r['n'] / total:.0f} %)")
    print(f"Gesamt: {total} Songs")


def cmd_make(args) -> None:
    conn = connect()
    rows = select_tracks(conn, args.min, args.max, extract_playlist_id(args.from_playlist)
                         if args.from_playlist else None, args.half_double, args.limit)
    if not rows:
        sys.exit("Keine passenden Songs gefunden (erst 'import' und 'lookup' ausführen?).")
    for r in rows:
        print(f"{r['bpm']:4} BPM  {r['artist']} – {r['title']}")
    print(f"{len(rows)} Songs zwischen {args.min} und {args.max} BPM.")
    if args.dry_run:
        print("Testlauf (--dry-run): es wurde nichts angelegt.")
        return
    name = args.name or f"BPM {args.min}-{args.max}"
    created = create_playlist(name, f"Automatisch erstellt: {args.min}-{args.max} BPM")
    add_items(created["id"], [r["uri"] for r in rows])
    link = created.get("external_urls", {}).get("spotify", created["id"])
    print(f"Playlist '{name}' angelegt: {link}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Playlists nach BPM erstellen")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="bei Spotify einloggen").set_defaults(func=cmd_login)
    sub.add_parser("playlists", help="eigene Playlists mit IDs anzeigen").set_defaults(func=cmd_playlists)
    p = sub.add_parser("import", help="Songs einer Playlist in die Datenbank laden")
    p.add_argument("playlists", nargs="+", help="Playlist-ID oder Link")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("lookup", help="BPM für alle Songs ohne BPM holen")
    p.add_argument("--refresh", action="store_true", help="auch bereits gesuchte neu abfragen")
    p.add_argument("--limit", type=int, help="nur die ersten N Songs (zum Testen)")
    p.set_defaults(func=cmd_lookup)
    sub.add_parser("stats", help="Abdeckung der BPM-Daten anzeigen").set_defaults(func=cmd_stats)
    p = sub.add_parser("make", help="neue Playlist aus BPM-Bereich erstellen")
    p.add_argument("--min", type=int, required=True)
    p.add_argument("--max", type=int, required=True)
    p.add_argument("--name")
    p.add_argument("--from-playlist", help="nur Songs aus dieser Playlist (ID oder Link)")
    p.add_argument("--half-double", action="store_true", help="auch halbes/doppeltes Tempo zulassen")
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts anlegen")
    p.set_defaults(func=cmd_make)
    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ApiError as err:
        hint = ""
        if err.status == 403:
            hint = ("\nHinweis: 403 heißt bei Spotify oft: Premium fehlt/abgelaufen, der Account ist "
                    "nicht als Nutzer der App eingetragen, oder die Playlist gehört dir nicht.")
        sys.exit(f"Fehler: {err}{hint}")


if __name__ == "__main__":
    main()
