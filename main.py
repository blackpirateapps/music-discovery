"""
music-discovery pipeline (v2.3)
───────────────────────────────
1. Fetches JSPF Recommendation Playlists.
2. Robust Fallback: If playlists are empty/None, fetches Top Tracks.
3. YouTube Search + GDrive Upload.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

import requests
import yt_dlp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# ──────────────────────────────────────────────
# Logging Setup
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("music-discovery")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
LB_API_BASE = "https://api.listenbrainz.org/1"
GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
ILLEGAL_CHARS_RE = re.compile(r'[\\/*?:"<>|]')

# ──────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────
def sanitize_filename(name: str) -> str:
    name = ILLEGAL_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def load_env(key: str, required: bool = True) -> str:
    value = os.getenv(key, "")
    if required and not value:
        logger.error("Required environment variable '%s' is not set.", key)
        sys.exit(1)
    return value

# ──────────────────────────────────────────────
# Phase 1 – ListenBrainz Client (Robust Fallback)
# ──────────────────────────────────────────────
class ListenBrainzClient:
    def __init__(self, token: str, username: str) -> None:
        self._token = token
        self._username = username
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Token {token}"})

    def fetch_recommendations(self, count: int = 15) -> list[dict]:
        """Attempt to fetch Playlists; Fallback to Top Tracks if needed."""
        logger.info("Phase 1 – Fetching music for user: %s", self._username)

        # Strategy A: Check Recommendation Playlists
        url = f"{LB_API_BASE}/user/{self._username}/playlists/recommendations"
        try:
            resp = self._session.get(url, timeout=20)
            if resp.status_code == 200:
                playlists = resp.json().get("playlists", [])
                if playlists:
                    # Pick the best looking playlist
                    target = next((p for p in playlists if "Daily" in (p.get("title") or "")), playlists[0])

                    # Extraction logic
                    mbid = target.get("playlist_mbid") or target.get("mbid")
                    if not mbid and "identifier" in target:
                        mbid = str(target["identifier"]).rstrip("/").split("/")[-1]

                    if mbid:
                        logger.info("Playlist found: '%s'", target.get('title'))
                        return self._fetch_playlist_tracks(mbid, count)
        except Exception as e:
            logger.debug("Playlist fetch failed: %s", e)

        # Strategy B: Fallback to Top Tracks (since Discovery can take 24h+ to process)
        logger.info("No valid playlists found yet. Falling back to Top Tracks...")
        return self._fetch_top_tracks(count)

    def _fetch_playlist_tracks(self, mbid: str, count: int) -> list[dict]:
        url = f"{LB_API_BASE}/playlist/{mbid}"
        resp = self._session.get(url)
        if resp.status_code != 200: return []

        tracks = resp.json().get("playlist", {}).get("track", [])
        return [{"artist_name": t.get("creator"), "track_name": t.get("title")} for t in tracks[:count]]

    def _fetch_top_tracks(self, count: int) -> list[dict]:
        """Fetches your most played tracks to ensure the script has something to do."""
        url = f"{LB_API_BASE}/stats/user/{self._username}/tracks"
        params = {"range": "all_time", "count": count}
        resp = self._session.get(url, params=params)

        if resp.status_code != 200:
            logger.error("Failed to fetch fallback tracks.")
            return []

        tracks = resp.json().get("payload", {}).get("tracks", [])
        return [{"artist_name": t.get("artist_name"), "track_name": t.get("track_name")} for t in tracks]

# ──────────────────────────────────────────────
# Phase 2 – Music Downloader
# ──────────────────────────────────────────────
class MusicDownloader:
    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    def download(self, artist: str, track: str) -> Optional[Path]:
        safe_name = sanitize_filename(f"{artist} - {track}")
        search_query = f"ytsearch1:{artist} - {track} (Official Audio)"
        output_template = str(self._output_dir / f"{safe_name}.%(ext)s")

        logger.info("Searching YouTube: %s", safe_name)
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": output_template,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            "noplaylist": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([search_query])
            expected = self._output_dir / f"{safe_name}.mp3"
            return expected if expected.exists() else None
        except Exception as e:
            logger.error("Download failed: %s", e)
            return None

# ──────────────────────────────────────────────
# Phase 3 – Google Drive Uploader
# ──────────────────────────────────────────────
class DriveUploader:
    def __init__(self, credentials_json: str, token_json: str, folder_id: str) -> None:
        self._folder_id = folder_id
        self._creds = self._build_credentials(credentials_json, token_json)
        self._service = build("drive", "v3", credentials=self._creds)

    def _build_credentials(self, credentials_json: str, token_json: str) -> Credentials:
        creds_info = json.loads(credentials_json)
        token_info = json.loads(token_json)
        creds = Credentials(
            token=token_info.get("token"),
            refresh_token=token_info.get("refresh_token"),
            token_uri=token_info.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=creds_info.get("installed", creds_info.get("web", {})).get("client_id"),
            client_secret=creds_info.get("installed", creds_info.get("web", {})).get("client_secret"),
            scopes=GDRIVE_SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds

    def file_exists(self, filename: str) -> bool:
        safe_name = filename.replace("'", "\\'")
        query = f"name = '{safe_name}' and '{self._folder_id}' in parents and trashed = false"
        result = self._service.files().list(q=query, fields="files(id)").execute()
        return bool(result.get("files"))

    def upload(self, file_path: Path) -> bool:
        if self.file_exists(file_path.name):
            logger.info("Already on Drive: %s", file_path.name)
            return True
        metadata = {"name": file_path.name, "parents": [self._folder_id]}
        media = MediaFileUpload(str(file_path), mimetype="audio/mpeg", resumable=True)
        self._service.files().create(body=metadata, media_body=media).execute()
        logger.info("Uploaded: %s", file_path.name)
        return True

# ──────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────
def main():
    logger.info("─── Starting Music Discovery Sync ───")
    lb_token = load_env("LB_TOKEN")
    lb_user = load_env("LB_USERNAME")
    drive_creds = load_env("GDRIVE_CREDENTIALS")
    drive_token = load_env("GDRIVE_TOKEN")
    drive_folder = load_env("GDRIVE_FOLDER_ID")

    lb = ListenBrainzClient(lb_token, lb_user)
    uploader = DriveUploader(drive_creds, drive_token, drive_folder)

    tracks = lb.fetch_recommendations(count=15)
    if not tracks:
        logger.info("No tracks found. Pipeline stopping.")
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        downloader = MusicDownloader(Path(tmp_dir))
        for t in tracks:
            artist, title = t["artist_name"], t["track_name"]
            filename = sanitize_filename(f"{artist} - {title}") + ".mp3"

            if not uploader.file_exists(filename):
                mp3_path = downloader.download(artist, title)
                if mp3_path:
                    uploader.upload(mp3_path)
                    if mp3_path.exists(): mp3_path.unlink()

    logger.info("─── Pipeline Finished ───")

if __name__ == "__main__":
    main()
