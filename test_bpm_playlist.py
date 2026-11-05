"""Tests für das BPM-Playlist-Tool.

Die Tests starten einen lokalen Fake-Server, der Spotify und GetSongBPM *so nachbildet, wie
die Dokumentation sie beschreibt*. Sie prüfen damit unsere Logik und den kompletten HTTP-Weg,
aber NICHT, ob die echten Dienste sich genauso verhalten.

Ausführen:  python3 test_bpm_playlist.py
"""
import contextlib
import hashlib
import base64
import http.server
import io
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

import bpm_playlist as bp


def cand(title, tempo, artist):
    return {"song_id": "x", "song_title": title, "tempo": tempo, "time_sig": "4/4",
            "key_of": "Am", "artist": {"id": "a", "name": artist}}


BPM_DATA = {
    ("dreams", "fleetwood mac"): [cand("Dreams of Tomorrow", "70", "Fleetwood Mac"),
                                  cand("Dreams", "90", "The Cranberries"),
                                  cand("Dreams", "120", "Fleetwood Mac")],
    ("levitating", "dua lipa"): [cand("Levitating", "103", "Dua Lipa")],
    ("billie jean", "michael jackson"): [cand("Billie Jean", "117", "Michael Jackson")],
    ("heart of glass", "blondie"): [cand("Heart of Glass", "230", "Blondie")],
}

ENTRIES = [
    {"item": {"id": "t1", "uri": "spotify:track:t1", "name": "Dreams - 2004 Remaster",
              "type": "track", "artists": [{"name": "Fleetwood Mac"}]}},
    {"item": {"id": "t2", "uri": "spotify:track:t2", "name": "Levitating (feat. DaBaby)",
              "type": "track", "artists": [{"name": "Dua Lipa"}, {"name": "DaBaby"}]}},
    {"item": {"id": None, "uri": "spotify:local:x", "name": "Lokale Datei", "is_local": True,
              "artists": []}},
    {"track": {"id": "t4", "uri": "spotify:track:t4", "name": "Billie Jean",  # alter Feldname
               "type": "track", "artists": [{"name": "Michael Jackson"}]}},
    {"item": {"id": "t5", "uri": "spotify:track:t5", "name": "Unknown Obscure Song",
              "type": "track", "artists": [{"name": "Nobody"}]}},
    {"item": {"id": "e1", "uri": "spotify:episode:e1", "name": "Podcast", "type": "episode",
              "artists": []}},
    {"item": {"id": "t7", "uri": "spotify:track:t7", "name": "Heart of Glass",
              "type": "track", "artists": [{"name": "Blondie"}]}},
]


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def st(self):
        return self.server.state

    def send_json(self, code, obj, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()

    def authed(self):
        if self.headers.get("Authorization") != "Bearer " + self.st["access"]:
            self.send_json(401, {"error": {"status": 401, "message": "bad token"}})
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/v1/me/playlists":
            if self.authed():
                self.send_json(200, {"items": [{"id": "PL1", "name": "Test", "items": {"total": 7}}],
                                     "next": None})
        elif re.fullmatch(r"/v1/playlists/\w+/items", parsed.path):
            if not self.authed():
                return
            offset = int(query.get("offset", ["0"])[0])
            page = ENTRIES[offset:offset + 3]
            nxt = None
            if offset + 3 < len(ENTRIES):
                host = self.headers["Host"]
                nxt = f"http://{host}{parsed.path}?limit=50&offset={offset + 3}"
            self.send_json(200, {"items": page, "next": nxt})
        elif parsed.path == "/bpm/search/":
            if self.headers.get("X-API-KEY") != "testkey":
                self.send_json(401, {"error": "no key"})
                return
            self.st["bpm_calls"] += 1
            if not self.st["bpm_429_done"]:
                self.st["bpm_429_done"] = True
                self.send_json(429, {"error": "slow down"}, {"Retry-After": "0"})
                return
            assert query["type"] == ["both"]
            match = re.fullmatch(r"song:(.*) artist:(.*)", query["lookup"][0])
            result = BPM_DATA.get((match.group(1).lower(), match.group(2).lower())) if match else None
            self.send_json(200, {"search": result if result else {"error": "no result"}})
        else:
            self.send_json(404, {"error": "unknown"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        raw = self.body()
        if parsed.path == "/api/token":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            if form["grant_type"] == "authorization_code":
                digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if challenge != self.st["challenge"] or form["code"] != "abc":
                    self.send_json(400, {"error": "invalid_grant"})
                    return
                self.st["access"] = "ACCESS1"
                self.send_json(200, {"access_token": "ACCESS1", "refresh_token": "REFRESH1",
                                     "expires_in": 3600})
            elif form["grant_type"] == "refresh_token" and form["refresh_token"] == "REFRESH1":
                self.st["refresh_calls"] += 1
                self.st["access"] = "ACCESS2"
                self.send_json(200, {"access_token": "ACCESS2", "expires_in": 3600})
            else:
                self.send_json(400, {"error": "invalid_grant"})
        elif parsed.path == "/v1/me/playlists":
            if self.authed():
                payload = json.loads(raw)
                self.st["created"].append(payload)
                self.send_json(201, {"id": "NEWPL", "external_urls": {
                    "spotify": "https://open.spotify.com/playlist/NEWPL"}})
        elif parsed.path == "/v1/playlists/NEWPL/items":
            if self.authed():
                uris = json.loads(raw)["uris"]
                assert len(uris) <= 100
                self.st["batches"].append(uris)
                self.send_json(201, {"snapshot_id": "s"})
        else:
            self.send_json(404, {"error": "unknown"})


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UnitTests(unittest.TestCase):
    def test_pkce_rfc7636_vector(self):
        # Testwert aus RFC 7636, Anhang B
        self.assertEqual(bp.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        verifier, _ = bp.make_pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)

    def test_clean_title(self):
        self.assertEqual(bp.clean_title("Dreams - 2004 Remaster"), "Dreams")
        self.assertEqual(bp.clean_title("Song - Remastered 2011"), "Song")
        self.assertEqual(bp.clean_title("Take On Me (Remastered 2015)"), "Take On Me")
        self.assertEqual(bp.clean_title("Levitating (feat. DaBaby)"), "Levitating")
        self.assertEqual(bp.clean_title("Soft Shock (Part 2)"), "Soft Shock (Part 2)")
        self.assertEqual(bp.clean_title("Monolith (Intro)"), "Monolith (Intro)")

    def test_normalize(self):
        self.assertEqual(bp.normalize("Beyoncé & Jay-Z"), "beyonce and jay z")
        self.assertEqual(bp.normalize_artist("The Cranberries"), "cranberries")

    def test_pick_best(self):
        best = bp.pick_best(BPM_DATA[("dreams", "fleetwood mac")], "Dreams - 2004 Remaster",
                            ["Fleetwood Mac"])
        self.assertEqual(best[2], 120)  # nicht 90 (falscher Künstler), nicht 70 (falscher Titel)
        self.assertIsNone(bp.pick_best([cand("Dreams", "90", "The Cranberries")], "Dreams",
                                       ["Fleetwood Mac"]))
        self.assertIsNone(bp.pick_best([cand("Dreams", "999", "Fleetwood Mac")], "Dreams",
                                       ["Fleetwood Mac"]))  # unplausibles Tempo

    def test_add_items_batches(self):
        server, base, state = start_fake()
        with env_for(base):
            write_token("ACCESS1")
            state["access"] = "ACCESS1"
            bp.add_items("NEWPL", [f"spotify:track:{i}" for i in range(250)])
        self.assertEqual([len(b) for b in state["batches"]], [100, 100, 50])
        server.shutdown()


def start_fake():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    server.state = {"access": "", "challenge": None, "refresh_calls": 0, "created": [],
                    "batches": [], "bpm_calls": 0, "bpm_429_done": False}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", server.state


TMP = Path(tempfile.mkdtemp())


def write_token(access, expires_in=3600):
    Path(os.environ["BPM_TOKEN_FILE"]).write_text(json.dumps({
        "access_token": access, "refresh_token": "REFRESH1", "expires_at": time.time() + expires_in}))


@contextlib.contextmanager
def env_for(base):
    values = {"SPOTIFY_API_BASE": base + "/v1", "SPOTIFY_ACCOUNTS_BASE": base,
              "GETSONGBPM_BASE": base + "/bpm", "SPOTIFY_CLIENT_ID": "cid",
              "GETSONGBPM_API_KEY": "testkey", "BPM_TOKEN_FILE": str(TMP / "token.json"),
              "BPM_DB": str(TMP / "test.db"), "SPOTIFY_REDIRECT_PORT": str(free_port())}
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        bp.main(list(argv))
    return out.getvalue()


class PipelineTest(unittest.TestCase):
    def test_full_pipeline(self):
        server, base, st = start_fake()
        self.addCleanup(server.shutdown)
        with env_for(base):
            # 1) Login mit PKCE: Fake-Browser ruft die Callback-Adresse auf
            def fake_browser(url):
                params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                st["challenge"] = params["code_challenge"][0]
                self.assertEqual(params["code_challenge_method"], ["S256"])
                self.assertEqual(params["redirect_uri"], [bp.redirect_uri()])

                def call():
                    time.sleep(0.3)
                    urllib.request.urlopen(f"{bp.redirect_uri()}?code=abc&state={params['state'][0]}")
                threading.Thread(target=call, daemon=True).start()

            token = bp.login(on_url=fake_browser, timeout=10)
            self.assertEqual(token["refresh_token"], "REFRESH1")

            # 2) Abgelaufenes Token wird automatisch erneuert
            write_token("ACCESS1", expires_in=-10)
            self.assertIn("PL1", run_cli("playlists"))
            self.assertEqual(st["refresh_calls"], 1)

            # 3) Import über mehrere Seiten; lokale Datei und Podcast werden übersprungen
            self.assertIn("5 Songs", run_cli("import", "https://open.spotify.com/playlist/PL1?si=abc"))
            conn = bp.connect()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 5)

            # 4) BPM-Abfrage (inkl. einmaligem 429 mit Wiederholung)
            bp.run_lookup(conn, delay=0)
            status = {r["spotify_id"]: (r["status"], r["bpm"]) for r in conn.execute("SELECT * FROM bpm")}
            self.assertEqual(status, {"t1": ("ok", 120), "t2": ("ok", 103), "t4": ("ok", 117),
                                      "t5": ("no_match", None), "t7": ("ok", 230)})
            calls = st["bpm_calls"]
            bp.run_lookup(conn, delay=0)  # alles im Cache -> keine neuen Anfragen
            self.assertEqual(st["bpm_calls"], calls)
            self.assertIn("no_match", run_cli("stats"))

            # 5) Auswahl nach BPM, mit und ohne Halbtempo/Doppeltempo
            def uris(low, high, **kw):
                return {r["uri"] for r in bp.select_tracks(conn, low, high, **kw)}
            self.assertEqual(uris(100, 110), {"spotify:track:t2"})
            self.assertEqual(uris(110, 120), {"spotify:track:t1", "spotify:track:t4"})
            self.assertEqual(uris(110, 120, half_double=True),
                             {"spotify:track:t1", "spotify:track:t4", "spotify:track:t7"})
            self.assertEqual(uris(110, 120, playlist="ANDERE"), set())

            # 6) Playlist anlegen
            out = run_cli("make", "--min", "110", "--max", "120", "--half-double", "--name", "Test")
            self.assertIn("NEWPL", out)
            self.assertEqual(st["created"][0]["name"], "Test")
            self.assertFalse(st["created"][0]["public"])
            self.assertEqual(sorted(st["batches"][-1]),
                             ["spotify:track:t1", "spotify:track:t4", "spotify:track:t7"])

            # 7) Testlauf legt nichts an
            before = len(st["created"])
            run_cli("make", "--min", "100", "--max", "110", "--dry-run")
            self.assertEqual(len(st["created"]), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
