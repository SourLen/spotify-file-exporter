# Spotify File Exporter

Turn a local MP3 collection into a Spotify playlist. The script reads track tags
with `ffprobe`, finds matching recordings in Spotify's catalog, and writes CSVs
for review before creating a playlist. It does not upload audio files.

## Setup

Install Python 3.10+ and [FFmpeg](https://ffmpeg.org/download.html) (which provides
`ffprobe`), then run:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
and register `http://127.0.0.1:8888/callback` as its redirect URI. Copy the app's
Client ID and Client Secret into your shell:

```sh
export SPOTIFY_CLIENT_ID="your-client-id"
export SPOTIFY_CLIENT_SECRET="your-client-secret"
```

For [development-mode apps](https://developer.spotify.com/documentation/web-api/concepts/quota-modes),
the app owner needs Spotify Premium; additional users must be added in the app's
User Management settings. Run `python spotify_import.py --help-setup` for setup
and optional credential-saving instructions.

## Usage

Run these commands from the project directory. First scan and match your music
without creating a playlist; a browser opens for Spotify authorization:

```sh
python spotify_import.py --music-dir "/path/to/mp3s" --dry-run
```

Review `matched.csv`, `unmatched.csv`, and, if generated,
`recovered_live_explicit.csv`. Matching is approximate; recovered tracks may use
a different recording. Then create a private playlist from the reviewed matches:

```sh
python spotify_import.py --from-matched matched.csv --playlist-name "My MP3 Library"
```

Add `--public` for a public playlist. Each creation run makes a new playlist.
Run `python spotify_import.py --help` for all options.

Scanned tags and search results are cached in `tracks.json` and
`search_cache.json` in the current directory. Repeat an interrupted matching
command to resume; use `--rescan` when changing the music directory or tags.
Generated reports, caches, and local credentials are excluded from Git. OAuth
tokens are stored in `~/.cache/spotify_mp3_import/token.json`.
