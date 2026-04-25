"""
music-discovery pipeline (v2.2)
───────────────────────────────
1. Fetches JSPF Recommendation Playlists from ListenBrainz.
2. Robustly extracts Playlist MBIDs from multiple possible JSON keys.
3. Downloads high-quality MP3s via yt-dlp.
4. Uploads to Google Drive with automatic OAuth2 token refresh.
5. Idempotent: Skips files already present on Drive.
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
    """Strip OS-illegal characters and collapse whitespace."""
    name = ILLEGAL_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def load_env(key: str, required: bool = True) -> str:
    """Read an env variable; raise clearly if required and missing."""
    value = os.getenv(key, "")
    if required and not value:
        logger.error("Required environment variable '%s' is not set.", key)
        sys.exit(1)
    return value

# ──────────────────────────────────────────────
# Phase 1 – ListenBrainz Client
# ──────────────────────────────────────────────
class ListenBrainzClient:
    """Handles fetching recommendation playlists with robust ID parsing."""

    def __init__(self, token: str, username: str) -> None:
        self._token = token
        self._username = username
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Token {token}"})

    def fetch_recommendations(self, count: int = 25) -> list[dict]:
        """Official Doc Reference: GET /1/user/{username}/playlists/recommendations"""
        logger.info("Phase 1 – Fetching playlists for user: %s", self._username)

        url = f"{LB_API_BASE}/user/{self._username}/playlists/recommendations"
        try:
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Failed to connect to ListenBrainz: %s", e)
            return []

        playlists = resp.json().get("playlists", [])
        if not playlists:
            logger.warning("No recommendation playlists found.")
            return []

        # Find 'Daily Discovery' first, fallback to the latest available (Weekly Jams)
        target = next((p for p in playlists if "Daily" in p.get("title", "")), playlists[0])

        # Robust MBID Extraction to prevent KeyErrors
        # 1. Try standard keys
        # 2. Try the 'identifier' URL (e.g., .../playlist/<UUID>)
        playlist_mbid = target.get("playlist_mbid") or target.get("mbid")

        if not playlist_mbid and "identifier" in target:
            playlist_mbid = target["identifier"].rstrip("/").split("/")[-1]

        if not playlist_mbid:
            logger.error("Could not extract a valid MBID for playlist: %s", target.get('title'))
            return []

        logger.info("Targeting Playlist: '%s' (ID: %s)", target.get('title'), playlist_mbid)

        # Fetch actual track data from the playlist endpoint
        track_url = f"{LB_API_BASE}/playlist/{playlist_mbid}"
        track_resp = self._session.get(track_url)

        if track_resp.status_code != 200:
            logger.error("Failed to fetch tracks for playlist %s", playlist_mbid)
            return []

        playlist_data = track_resp.json().get("playlist", {})
        tracks_list = playlist_data.get("track", [])

        results = []
        for t in tracks_list[:count]:
            # JSPF 'track' objects use 'creator' for Artist and 'title' for Track name
            artist = t.get("creator")
            title = t.get("title")
            if artist and title:
                results.append({"artist_name": artist, "track_name": title})

        return results

# ──────────────────────────────────────────────
# Phase 2 – Music Downloader
# ──────────────────────────────────────────────
class MusicDownloader:
    """Downloads a single track as MP3 using yt-dlp."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    def download(self, artist: str, track: str) -> Optional[Path]:
        safe_name = sanitize_filename(f"{artist} - {track}")
        search_query = f"ytsearch1:{artist} - {track} (Official Audio)"
        output_template = str(self._output_dir / f"{safe_name}.%(ext)s")

        logger.info("Phase 2 – Searching YouTube: %s", safe_name)

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": output_template,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "noplaylist": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([search_query])

            expected_file = self._output_dir / f"{safe_name}.mp3"
            return expected_file if expected_file.exists() else None
        except Exception as e:
            logger.error("Download failed for %s: %s", safe_name, e)
            return None

# ──────────────────────────────────────────────
# Phase 3 – Google Drive Uploader
# ──────────────────────────────────────────────
class DriveUploader:
    """Handles idempotent upload with automatic token refresh."""

    def __init__(self, credentials_json: str, token_json: str, folder_id: str) -> None:
        self._folder_id = folder_id
        self._creds = self._build_credentials(credentials_json, token_json)
        self._service = build("drive", "v3", credentials=self._creds)

    def _build_credentials(self, credentials_json: str, token_json: str) -> Credentials:
        creds_info = json.loads(credentials_json)
        token_info = json.loads(token_json)

        # Standard OAuth2 credential object
        creds = Credentials(
            token=token_info.get("token"),
            refresh_token=token_info.get("refresh_token"),
            token_uri=token_info.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=creds_info.get("installed", creds_info.get("web", {})).get("client_id"),
            client_secret=creds_info.get("installed", creds_info.get("web", {})).get("client_secret"),
            scopes=GDRIVE_SCOPES,
        )

        # Automatically refresh if expired
        if creds.expired and creds.refresh_token:
            logger.info("Phase 3 – Refreshing Google Drive session...")
            creds.refresh(Request())

        return creds

    def file_exists(self, filename: str) -> bool:
        """Check if file exists in the target Drive folder."""
        # Drive API escape character for single quotes
        safe_name = filename.replace("'", "\\'")
        query = f"name = '{safe_name}' and '{self._folder_id}' in parents and trashed = false"

        try:
            result = self._service.files().list(q=query, fields="files(id)").execute()
            return bool(result.get("files"))
        except HttpError as e:
            logger.error("Drive existence check failed: %s", e)
            return False

    def upload(self, file_path: Path) -> bool:
        filename = file_path.name
        if self.file_exists(filename):
            logger.info("File '%s' already exists on Drive. Skipping.", filename)
            return True

        metadata = {"name": filename, "parents": [self._folder_id]}
        media = MediaFileUpload(str(file_path), mimetype="audio/mpeg", resumable=True)

        try:
            self._service.files().create(body=metadata, media_body=media).execute()
            logger.info("Successfully uploaded: %s", filename)
            return True
        except HttpError as e:
            logger.error("Upload failed: %s", e)
            return False

# ──────────────────────────────────────────────
# Main Orchestrator
# ──────────────────────────────────────────────
def main():
    logger.info("─── Starting Discovery Sync ───")

    # Load Environment Secrets
    lb_token = load_env("LB_TOKEN")
    lb_user = load_env("LB_USERNAME")
    drive_creds = load_env("GDRIVE_CREDENTIALS")
    drive_token = load_env("GDRIVE_TOKEN")
    drive_folder = load_env("GDRIVE_FOLDER_ID")

    # Initialize Clients
    lb = ListenBrainzClient(lb_token, lb_user)
    uploader = DriveUploader(drive_creds, drive_token, drive_folder)

    # 1. Fetch Playlist
    tracks = lb.fetch_recommendations(count=15)
    if not tracks:
        logger.info("No recommendations found. Pipeline stopping.")
        return

    logger.info("Found %d tracks to process.", len(tracks))

    # 2. Process Tracks in a Temporary Directory
    with tempfile.TemporaryDirectory() as tmp_dir:
        downloader = MusicDownloader(Path(tmp_dir))

        for t in tracks:
            artist, title = t["artist_name"], t["track_name"]
            filename = sanitize_filename(f"{artist} - {title}") + ".mp3"

            # Pre-download Check
            if uploader.file_exists(filename):
                logger.info("Skipping '%s' (Already on Drive).", filename)
                continue

            # Download locally
            mp3_path = downloader.download(artist, title)
            if mp3_path:
                # Upload to Drive
                uploader.upload(mp3_path)
                # Immediate cleanup
                if mp3_path.exists():
                    mp3_path.unlink()

    logger.info("─── Pipeline Finished Successfully ───")

if __name__ == "__main__":
    main()
