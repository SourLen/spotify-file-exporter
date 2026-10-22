#!/usr/bin/env python3
"""
spotify_import.py — turn a folder of local MP3s into a real Spotify playlist.

It reads the ID3 tags (title/artist) from every .mp3 under a directory, searches
the Spotify catalog for each track, and adds the best matches to a playlist on
your account. The result is normal Spotify tracks, not local files.

Requires Python 3.10+, `ffprobe` (from FFmpeg), and the `requests` Python package.
Install the Python dependency with: python3 -m pip install -r requirements.txt

Typical usage
-------------
    export SPOTIFY_CLIENT_ID=xxxxxxxx
    export SPOTIFY_CLIENT_SECRET=yyyyyyyy

    # 1. Dry run: match everything, write matched.csv / unmatched.csv, change nothing
    python3 spotify_import.py --dry-run

    # 2. Review the CSVs, then do it for real
    python3 spotify_import.py --playlist-name "My MP3 Library"

Set up the Spotify app first (see the README notes printed by --help-setup).
"""

import argparse
import base64
import csv
import difflib
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "playlist-modify-private playlist-modify-public"

DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
TOKEN_CACHE = Path.home() / ".cache" / "spotify_mp3_import" / "token.json"
CONFIG_FILE = Path.home() / ".config" / "spotify_mp3_import" / "config"
TRACKS_CACHE = Path("tracks.json")
SEARCH_CACHE = Path("search_cache.json")   # remembers lookups so runs can resume

MAX_INLINE_WAIT = 30   # seconds; a longer 429 aborts the run so we can resume later

# Parenthetical/bracket noise we strip from titles before searching so that
# "Heart Of Glass [24-Bit Digitally Remastered 01] (Remastered 2001)" still
# matches the plain catalog title. "live"/"acoustic"/"demo" are kept — they
# describe genuinely different recordings.
NOISE_RE = re.compile(
    r"""\s*[\(\[]           # opening ( or [
        [^)\]]*             # anything inside
        \b(?:remaster(?:ed)?|mono|stereo|digitally|\d+\s*-?\s*bit|
             single\ version|album\ version|bonus|deluxe|anniversary|
             reissue|re-?master|hd)\b
        [^)\]]*
        [\)\]]              # closing ) or ]
    """,
    re.IGNORECASE | re.VERBOSE,
)


# --------------------------------------------------------------------------- #
# Placeholder tags -> fall back to Artist/Album/Track from the file path
# --------------------------------------------------------------------------- #
# Some rips (e.g. the Eagles "Hotel California" folder) carry generic German or
# English placeholder tags — "Unbekannter Interpret", "Unbekanntes Album (...)",
# "Titelnummer 1" — instead of real metadata. Only for those do we fall back to
# the folder layout  <Artist>/<Album>/<NN Track name>.mp3 ; files with genuine
# tags are left untouched.
UNKNOWN_RE = re.compile(r"\b(?:unbekannt\w*|unknown)\b", re.IGNORECASE)
TITLE_PLACEHOLDER_RE = re.compile(
    r"^(?:titel(?:nummer)?|track|audio\s*track|spur|title)\s*\d*$", re.IGNORECASE)


def is_placeholder(value: str, is_title: bool = False) -> bool:
    v = value.strip()
    if not v:
        return True
    if UNKNOWN_RE.search(v):
        return True
    if is_title and (TITLE_PLACEHOLDER_RE.match(v) or v.isdigit()):
        return True
    return False


def title_from_filename(path: str) -> str:
    """'01 Hotel California.mp3' / '03 - Life In The Fast Lane.mp3' -> title."""
    stem = Path(path).stem
    stem = re.sub(r"^\s*\d{1,3}\s*[-.\)]*\s*", "", stem)  # strip leading track no.
    return stem.strip()


def path_context(path: str, music_dir: str) -> tuple[str, str]:
    """Infer (artist, album) from folders: <Artist>/<Album>/file."""
    folders = os.path.relpath(path, music_dir).split(os.sep)[:-1]
    artist = folders[0] if folders else ""
    album = folders[-1] if len(folders) >= 2 else ""
    return artist, album


# --------------------------------------------------------------------------- #
# Tag scanning
# --------------------------------------------------------------------------- #
def scan_tags(music_dir: str) -> list[dict]:
    """Walk music_dir and pull (title, artist, album) from each mp3 via ffprobe."""
    mp3s = sorted(
        os.path.join(r, f)
        for r, _, files in os.walk(music_dir)
        for f in files
        if f.lower().endswith(".mp3")
    )
    tracks, n_repaired = [], 0
    for i, path in enumerate(mp3s, 1):
        out = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-show_entries", "format_tags=title,artist,album",
             "-of", "json", path],
            capture_output=True, text=True,
        ).stdout
        try:
            tags = json.loads(out).get("format", {}).get("tags", {})
        except json.JSONDecodeError:
            tags = {}
        tags = {k.lower(): v for k, v in tags.items()}
        title = (tags.get("title") or "").strip()
        artist = (tags.get("artist") or "").strip()
        album = (tags.get("album") or "").strip()

        # Repair placeholder ("Unknown"-style) fields from the folder layout.
        p_artist, p_album = path_context(path, music_dir)
        repaired = False
        if is_placeholder(artist) and p_artist:
            artist, repaired = p_artist, True
        if is_placeholder(title, is_title=True):
            title, repaired = title_from_filename(path), True
        if is_placeholder(album) and p_album:
            album, repaired = p_album, True
        n_repaired += repaired

        tracks.append({"path": path, "title": title,
                       "artist": artist, "album": album})
        if i % 50 == 0 or i == len(mp3s):
            print(f"  scanned {i}/{len(mp3s)} files", end="\r", flush=True)
    print()
    if n_repaired:
        print(f"  ({n_repaired} files had placeholder tags rebuilt from the "
              f"Artist/Album/Track folder layout)")
    return tracks


# --------------------------------------------------------------------------- #
# Matching helpers
# --------------------------------------------------------------------------- #
def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"[^\w\s]", " ", s)          # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", NOISE_RE.sub("", title)).strip() or title


def artist_ok(local_artist: str, cand_artists: str) -> bool:
    a, b = normalize(local_artist), normalize(cand_artists)
    if not a:
        return True
    if a in b or b in a:
        return True
    at, bt = set(a.split()), set(b.split())
    return bool(at) and len(at & bt) / len(at) >= 0.5


def score(local_title: str, cand_title: str) -> float:
    return difflib.SequenceMatcher(
        None, normalize(clean_title(local_title)), normalize(clean_title(cand_title))
    ).ratio()


# --------------------------------------------------------------------------- #
# OAuth (Authorization Code flow with a loopback callback server)
# --------------------------------------------------------------------------- #
class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):  # noqa: N802
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        _CallbackHandler.code = params.get("code", [None])[0]
        _CallbackHandler.error = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "You can close this tab and return to the terminal."
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *_):  # silence the default logging
        pass


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def get_token(client_id: str, client_secret: str, redirect_uri: str) -> str:
    """Return a valid access token, refreshing or running consent as needed."""
    cache = {}
    if TOKEN_CACHE.exists():
        cache = json.loads(TOKEN_CACHE.read_text())

    # Fast path: unexpired cached access token.
    if cache.get("access_token") and cache.get("expires_at", 0) > time.time() + 60:
        return cache["access_token"]

    # Try to refresh silently.
    if cache.get("refresh_token"):
        resp = requests.post(
            TOKEN_URL,
            headers={"Authorization": _basic_auth(client_id, client_secret)},
            data={"grant_type": "refresh_token",
                  "refresh_token": cache["refresh_token"]},
        )
        if resp.ok:
            tok = resp.json()
            cache["access_token"] = tok["access_token"]
            cache["expires_at"] = time.time() + tok.get("expires_in", 3600)
            if tok.get("refresh_token"):
                cache["refresh_token"] = tok["refresh_token"]
            _save_token(cache)
            return cache["access_token"]

    # Full consent: open browser, catch the redirect on the loopback server.
    parsed = urllib.parse.urlparse(redirect_uri)
    port = parsed.port or 8888
    state = base64.urlsafe_b64encode(os.urandom(9)).decode()
    authorize = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    })

    server = http.server.HTTPServer((parsed.hostname or "127.0.0.1", port),
                                    _CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("\nOpening your browser to authorize Spotify access…")
    print("If it doesn't open, paste this URL manually:\n  " + authorize + "\n")
    webbrowser.open(authorize)

    # Wait for the callback thread to capture a code.
    for _ in range(300):
        if _CallbackHandler.code or _CallbackHandler.error:
            break
        time.sleep(1)
    server.server_close()

    if _CallbackHandler.error or not _CallbackHandler.code:
        sys.exit(f"Authorization failed: {_CallbackHandler.error or 'no code received'}")

    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": _basic_auth(client_id, client_secret)},
        data={"grant_type": "authorization_code",
              "code": _CallbackHandler.code,
              "redirect_uri": redirect_uri},
    )
    resp.raise_for_status()
    tok = resp.json()
    cache = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + tok.get("expires_in", 3600),
    }
    _save_token(cache)
    return cache["access_token"]


def _save_token(cache: dict) -> None:
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(cache))
    os.chmod(TOKEN_CACHE, 0o600)


# --------------------------------------------------------------------------- #
# Spotify API calls (with basic 429 handling)
# --------------------------------------------------------------------------- #
class RateLimited(Exception):
    """Raised on a 429 whose Retry-After is too long to wait out inline."""
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Spotify rate limit; retry after {retry_after}s")


def api(method: str, url: str, token: str, **kw):
    for _ in range(6):
        resp = requests.request(
            method, url, headers={"Authorization": f"Bearer {token}"}, **kw
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2")) + 1
            if wait > MAX_INLINE_WAIT:      # e.g. Spotify's ~23h dev-mode cooldown
                raise RateLimited(wait)
            print(f"  rate limited, waiting {wait}s…")
            time.sleep(wait)
            continue
        if not resp.ok:
            hint = ""
            if resp.status_code == 403:
                hint = ("\n  403 usually means the logged-in account is not "
                        "allow-listed on this app (Dashboard > Settings > User "
                        "Management) while it is in Development Mode.")
            raise RuntimeError(
                f"Spotify API {resp.status_code} on {method} {url}\n"
                f"  {resp.text[:400]}{hint}")
        return resp.json() if resp.text else {}
    raise RuntimeError("Too many rate-limit retries")


def search_track(local: dict, token: str, market: str) -> dict | None:
    """Return {name, artist, uri, score} for the best catalog match, or None."""
    title, artist = local["title"], local["artist"]
    ct = clean_title(title)
    queries = [
        f'track:"{ct}" artist:"{artist}"',
        f"{artist} {ct}",
    ]
    seen, candidates = set(), []
    for q in queries:
        data = api("GET", f"{API}/search", token, params={
            "q": q, "type": "track", "limit": 8, "market": market,
        })
        for item in data.get("tracks", {}).get("items", []):
            if item["uri"] in seen:
                continue
            seen.add(item["uri"])
            candidates.append(item)
        if candidates:
            break  # first (filtered) query is usually enough

    best, best_score = None, 0.0
    for item in candidates:
        cand_artists = ", ".join(a["name"] for a in item["artists"])
        if not artist_ok(artist, cand_artists):
            continue
        s = score(title, item["name"])
        # nudge toward same-artist exactness
        if normalize(artist) in normalize(cand_artists):
            s += 0.05
        if s > best_score:
            best, best_score = item, s

    if best and best_score >= 0.55:
        return {
            "name": best["name"],
            "artist": ", ".join(a["name"] for a in best["artists"]),
            "uri": best["uri"],
            "score": round(min(best_score, 1.0), 3),
        }
    return None


LIVE_EXPLICIT_RE = re.compile(r"\b(?:live|explicit)\b", re.IGNORECASE)


def strip_live_explicit(title: str) -> str:
    """Remove live/explicit markers: '(Live)', '[Explicit]', '- Live at ...'."""
    s = re.sub(r"\s*[\(\[][^)\]]*\b(?:live|explicit)\b[^)\]]*[\)\]]", "", title,
               flags=re.IGNORECASE)
    s = re.sub(r"\s*[-–]\s*(?:live|explicit)\b.*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\b(?:live|explicit)\b\s*$", "", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip() or title


def retry_live_explicit(unmatched: list[dict], search_fn):
    """Second-chance pass over unmatched tracks whose title/album mentions
    'live' or 'explicit': search again (via search_fn) with that marker removed.

    Returns (recovered, still_unmatched), where recovered is a list of
    (local, hit). These are flagged for the user because a live recording may
    have been substituted with a studio version (or vice versa)."""
    recovered, still = [], []
    for t in unmatched:
        blob = f"{t['title']} {t.get('album', '')}"
        stripped = strip_live_explicit(t["title"])
        # Only worth retrying if a marker is present AND stripping changed the
        # title (otherwise the query is identical to the one that already failed).
        if not LIVE_EXPLICIT_RE.search(blob) or stripped == t["title"]:
            still.append(t)
            continue
        hit = search_fn({**t, "title": stripped})
        if hit:
            recovered.append((t, hit))
        else:
            still.append(t)
    return recovered, still


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _parse_env_file(path: Path) -> dict:
    """Read simple KEY=VALUE lines (optional 'export ' prefix, # comments)."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[len("export "):].strip() if line.startswith("export ") else line
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip("'\"")
    return values


def load_credentials() -> tuple[str | None, str | None]:
    """Resolve client id/secret: environment first, then saved config file,
    then a local .env in the working directory."""
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    for path in (CONFIG_FILE, Path(".env")):
        if path.exists():
            vals = _parse_env_file(path)
            cid = cid or vals.get("SPOTIFY_CLIENT_ID")
            secret = secret or vals.get("SPOTIFY_CLIENT_SECRET")
            if cid and secret:
                return cid, secret
    return cid, secret


def save_credentials(cid: str, secret: str) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        f"SPOTIFY_CLIENT_ID={cid}\nSPOTIFY_CLIENT_SECRET={secret}\n")
    os.chmod(CONFIG_FILE, 0o600)
    print(f"Saved credentials to {CONFIG_FILE} (readable only by you). "
          "No need to export them again.")


SETUP = """\
Spotify app setup (one time)
============================
1. Go to https://developer.spotify.com/dashboard and log in.
2. Click "Create app". Name/description can be anything.
3. Under "Redirect URIs" add exactly:  http://127.0.0.1:8888/callback
   (must match --redirect-uri; loopback 127.0.0.1 is required, not localhost)
4. Save. Open the app's Settings to copy the Client ID and Client Secret.
5. Save them ONCE so you never have to export again:
       SPOTIFY_CLIENT_ID=...id... SPOTIFY_CLIENT_SECRET=...secret... \\
           python3 spotify_import.py --save-keys
   (stored in ~/.config/spotify_mp3_import/config, readable only by you)

   Prefer env vars instead? They also work, but only for the current shell:
       export SPOTIFY_CLIENT_ID=...id...
       export SPOTIFY_CLIENT_SECRET=...secret...
"""


def uris_from_matched(csv_path: str) -> list[str]:
    """Read unique spotify:track: URIs from a matched.csv, preserving order."""
    uris, seen = [], set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            u = (row.get("uri") or "").strip()
            if u.startswith("spotify:track:") and u not in seen:
                seen.add(u)
                uris.append(u)
    return uris


def create_playlist(token: str, me: dict, name: str, public: bool,
                    uris: list[str]) -> str:
    """Create a playlist and add all uris (100 per request). Returns the URL.

    Uses the post-February-2026 endpoints (POST /me/playlists and
    /playlists/{id}/items); the old /users/{id}/playlists and /tracks routes
    were retired for Development-Mode apps and now return 403."""
    playlist = api("POST", f"{API}/me/playlists", token, json={
        "name": name,
        "public": public,
        "description": f"Imported from local MP3s ({len(uris)} tracks).",
    })
    url = playlist["external_urls"]["spotify"]
    print(f"Created playlist: {playlist['name']}  ({url})")
    for start in range(0, len(uris), 100):
        api("POST", f"{API}/playlists/{playlist['id']}/items", token,
            json={"uris": uris[start:start + 100]})
        print(f"  added {min(start + 100, len(uris))}/{len(uris)}")
    return url


def create_from_matched(args) -> None:
    """Build the playlist straight from an existing matched.csv — no searching."""
    csv_path = args.from_matched
    if not Path(csv_path).exists():
        sys.exit(f"{csv_path} not found — run a normal (dry-)run first to produce it.")
    uris = uris_from_matched(csv_path)
    if not uris:
        sys.exit(f"No spotify:track: URIs found in {csv_path}.")

    cid, secret = load_credentials()
    if not (cid and secret):
        sys.exit("No Spotify credentials found (see --help-setup / --save-keys).")
    token = get_token(cid, secret, args.redirect_uri)
    me = api("GET", f"{API}/me", token)
    print(f"Authorized as {me.get('display_name') or me['id']} "
          f"(id {me['id']}) — creating '{args.playlist_name}' with {len(uris)} tracks.")
    create_playlist(token, me, args.playlist_name, args.public, uris)
    print("\nDone. Open the playlist link above in Spotify.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--music-dir", default=".", help="folder to scan (default: .)")
    ap.add_argument("--playlist-name", default="MP3 Import")
    ap.add_argument("--public", action="store_true", help="make the playlist public")
    ap.add_argument("--dry-run", action="store_true",
                    help="match only; write CSVs; create nothing on Spotify")
    ap.add_argument("--rescan", action="store_true", help="re-read tags, ignore cache")
    ap.add_argument("--limit", type=int, default=0, help="only process first N tracks")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds to pause between searches (default 0.5) to "
                         "avoid Spotify rate limits")
    ap.add_argument("--redirect-uri", default=DEFAULT_REDIRECT)
    ap.add_argument("--help-setup", action="store_true", help="print app setup steps")
    ap.add_argument("--save-keys", action="store_true",
                    help="save SPOTIFY_CLIENT_ID/SECRET to a config file, then exit")
    ap.add_argument("--from-matched", metavar="CSV", nargs="?", const="matched.csv",
                    help="skip scanning/searching: build the playlist straight from "
                         "an existing matched.csv (default: matched.csv)")
    args = ap.parse_args()

    if args.help_setup:
        print(SETUP)
        return

    if args.save_keys:
        cid, secret = load_credentials()
        if not (cid and secret):
            sys.exit("Provide the keys to save, e.g.:\n"
                     "  SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... "
                     "python3 spotify_import.py --save-keys")
        save_credentials(cid, secret)
        return

    if args.from_matched:
        create_from_matched(args)
        return

    # 1. Tags (cached to tracks.json).
    if args.rescan or not TRACKS_CACHE.exists():
        print("Scanning ID3 tags…")
        tracks = scan_tags(args.music_dir)
        TRACKS_CACHE.write_text(json.dumps(tracks, indent=2, ensure_ascii=False))
    else:
        tracks = json.loads(TRACKS_CACHE.read_text())
        print(f"Loaded {len(tracks)} tracks from {TRACKS_CACHE} (use --rescan to refresh)")
    if args.limit:
        tracks = tracks[:args.limit]

    # 2. Auth (skipped for a pure --dry-run only if you also want no market;
    #    we still auth so search uses your market and matches are playable).
    cid, secret = load_credentials()
    if not (cid and secret):
        sys.exit("No Spotify credentials found. Save them once with:\n"
                 "  SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... "
                 "python3 spotify_import.py --save-keys\n"
                 "(or run: python3 spotify_import.py --help-setup)")
    token = get_token(cid, secret, args.redirect_uri)
    me = api("GET", f"{API}/me", token)
    market = me.get("country", "US")
    print(f"Authorized as {me.get('display_name') or me['id']} (market {market})")

    # 3. Match. Every lookup is cached to SEARCH_CACHE so an interrupted or
    #    rate-limited run can be resumed by re-running the same command.
    cache = json.loads(SEARCH_CACHE.read_text()) if SEARCH_CACHE.exists() else {}

    def do_search(local: dict):
        key = normalize(local["artist"]) + "\t" + normalize(local["title"])
        if key in cache:                    # already looked up (hit or no-match)
            return cache[key]
        result = search_track(local, token, market)   # may raise RateLimited
        cache[key] = result
        time.sleep(args.delay)
        return result

    if cache:
        print(f"Resuming: {len(cache)} lookups already cached in {SEARCH_CACHE}.")
    print(f"\nSearching Spotify for {len(tracks)} tracks…")
    matched, unmatched, uris = [], [], []
    try:
        for i, t in enumerate(tracks, 1):
            if not t["title"]:
                unmatched.append(t)
                continue
            hit = do_search(t)
            if hit:
                matched.append((t, hit))
                if hit["uri"] not in uris:
                    uris.append(hit["uri"])
            else:
                unmatched.append(t)
            if i % 25 == 0:
                SEARCH_CACHE.write_text(json.dumps(cache))
            print(f"  {i}/{len(tracks)}  matched={len(matched)}  "
                  f"unmatched={len(unmatched)}", end="\r", flush=True)
        print()
        # Second chance: retry unmatched live/explicit tracks with marker removed.
        recovered, unmatched = retry_live_explicit(unmatched, do_search)
    except RateLimited as e:
        SEARCH_CACHE.write_text(json.dumps(cache))
        sys.exit(
            f"\n\nSpotify rate-limited this app and asked us to wait "
            f"{e.retry_after}s (~{e.retry_after / 3600:.1f}h) — this is normal for "
            f"a Development-Mode app.\nProgress saved: {len(cache)} lookups cached "
            f"in {SEARCH_CACHE}.\nWait a while, then re-run the SAME command — it "
            f"resumes from the cache and only searches what's left. Increasing "
            f"--delay helps avoid hitting the limit again.")
    SEARCH_CACHE.write_text(json.dumps(cache))
    for local, hit in recovered:
        matched.append((local, hit))
        if hit["uri"] not in uris:
            uris.append(hit["uri"])

    with open("matched.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["local_artist", "local_title", "spotify_artist",
                    "spotify_title", "score", "uri", "path"])
        for local, hit in matched:
            w.writerow([local["artist"], local["title"], hit["artist"],
                        hit["name"], hit["score"], hit["uri"], local["path"]])
    with open("unmatched.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["artist", "title", "album", "path"])
        for u in unmatched:
            w.writerow([u["artist"], u["title"], u["album"], u["path"]])

    print(f"\nMatched {len(matched)}/{len(tracks)}  "
          f"({len(uris)} unique tracks). See matched.csv / unmatched.csv.")

    # Tell the user which songs were only found after dropping live/explicit.
    if recovered:
        with open("recovered_live_explicit.csv", "w", newline="",
                  encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["local_artist", "local_title", "local_album",
                        "matched_artist", "matched_title", "score", "uri"])
            for local, hit in recovered:
                w.writerow([local["artist"], local["title"], local["album"],
                            hit["artist"], hit["name"], hit["score"], hit["uri"]])
        print(f"\n⚠  {len(recovered)} song(s) were matched only after removing a "
              "'live'/'explicit' marker — verify these are the versions you want\n"
              "   (also saved to recovered_live_explicit.csv):")
        for local, hit in recovered:
            print(f"   • {local['artist']} — {local['title']}")
            print(f"       -> {hit['artist']} — {hit['name']}  (score {hit['score']})")

    if args.dry_run:
        print("Dry run — no playlist created. Re-run without --dry-run to push.")
        return

    # 4. Create playlist + add tracks.
    create_playlist(token, me, args.playlist_name, args.public, uris)
    print("\nDone. Open the playlist link above in Spotify.")


if __name__ == "__main__":
    main()
