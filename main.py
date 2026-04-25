"""
music-discovery pipeline
────────────────────────
Fetches ListenBrainz recommendations → downloads MP3 via yt-dlp →
uploads to Google Drive (idempotent) → cleans up local files.
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
    """Thin wrapper around the ListenBrainz HTTP API."""

    def __init__(self, token: str, username: str) -> None:
        self._token = token
        self._username = username
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Token {token}"})

    def _get(self, path: str, **params) -> Optional[dict]:
        url = f"{LB_API_BASE}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=30)
            if resp.status_code == 204:
                logger.info("ListenBrainz returned 204 – no recommendations ready.")
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("ListenBrainz request failed: %s", exc)
            return None

    def fetch_recommendations(self, count: int = 25) -> list[dict]:
        """Return a list of {recording_mbid, artist_name, track_name} dicts."""
        data = self._get(
            f"/cf/recommendation/user/{self._username}/recording",
            count=count,
        )
        if data is None:
            return []

        mbids: list[str] = [
            rec["recording_mbid"]
            for rec in data.get("payload", {}).get("mbid_mapping", [])
            if rec.get("recording_mbid")
        ]

        tracks: list[dict] = []
        for mbid in mbids:
            meta = self._lookup_metadata(mbid)
            if meta:
                tracks.append(meta)
        return tracks

    def _lookup_metadata(self, mbid: str) -> Optional[dict]:
        data = self._get("/metadata/lookup/", recording_mbid=mbid)
        if data is None:
            return None
        artist = data.get("artist_credit_name") or data.get("artist_name", "")
        track = data.get("recording_name") or data.get("track_name", "")
        if not artist or not track:
            logger.warning("Incomplete metadata for MBID %s – skipping.", mbid)
            return None
        return {"recording_mbid": mbid, "artist_name": artist, "track_name": track}


# ──────────────────────────────────────────────
# Phase 2 – Music Downloader
# ──────────────────────────────────────────────
class MusicDownloader:
    """Downloads a single track as MP3 using yt-dlp."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    def _build_options(self, output_template: str) -> dict:
        return {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": output_template,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            # Avoid interactive prompts
            "noplaylist": True,
        }

    def download(self, artist: str, track: str) -> Optional[Path]:
        """
        Search YouTube for the track and download it as MP3.
        Returns the Path to the downloaded file, or None on failure.
        """
        safe_name = sanitize_filename(f"{artist} - {track}")
        search_query = f"ytsearch1:{artist} - {track} (Official Audio)"
        output_template = str(self._output_dir / f"{safe_name}.%(ext)s")

        logger.info("Downloading: %s", safe_name)
        options = self._build_options(output_template)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([search_query])
        except yt_dlp.utils.DownloadError as exc:
            logger.error("yt-dlp download failed for '%s': %s", safe_name, exc)
            return None

        # yt-dlp writes the final file; find it
        expected = self._output_dir / f"{safe_name}.mp3"
        if expected.exists():
            return expected

        # Fallback: scan for any mp3 with the sanitized base name
        candidates = list(self._output_dir.glob(f"{safe_name}*.mp3"))
        if candidates:
            return candidates[0]

        logger.error("Downloaded file not found for '%s'.", safe_name)
        return None


# ──────────────────────────────────────────────
# Phase 3 – Google Drive Uploader
# ──────────────────────────────────────────────
class DriveUploader:
    """Handles idempotent upload to a Google Drive folder."""

    def __init__(self, credentials_json: str, token_json: str, folder_id: str) -> None:
        self._folder_id = folder_id
        self._creds = self._build_credentials(credentials_json, token_json)
        self._service = build("drive", "v3", credentials=self._creds)

    # ------------------------------------------------------------------
    # Credential handling
    # ------------------------------------------------------------------
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
            logger.info("Access token expired – refreshing…")
            creds.refresh(Request())
            logger.info("Token refreshed successfully.")

        return creds

    # ------------------------------------------------------------------
    # Idempotency check
    # ------------------------------------------------------------------
    def file_exists(self, filename: str) -> bool:
        """Return True if a file with *filename* already exists in the folder."""
        safe_name = filename.replace("'", "\\'")
        query = (
            f"name = '{safe_name}' "
            f"and '{self._folder_id}' in parents "
            f"and trashed = false"
        )
        try:
            result = (
                self._service.files()
                .list(q=query, fields="files(id, name)", spaces="drive")
                .execute()
            )
            return bool(result.get("files"))
        except HttpError as exc:
            logger.error("Drive API error during existence check: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------
    def upload(self, file_path: Path) -> bool:
        """Upload *file_path* to Drive. Returns True on success."""
        filename = file_path.name

        if self.file_exists(filename):
            logger.info("'%s' already exists on Drive – skipping upload.", filename)
            return True  # idempotent success

        file_metadata = {"name": filename, "parents": [self._folder_id]}
        media = MediaFileUpload(str(file_path), mimetype="audio/mpeg", resumable=True)

        try:
            uploaded = (
                self._service.files()
                .create(body=file_metadata, media_body=media, fields="id")
                .execute()
            )
            logger.info(
                "Uploaded '%s' → Drive file ID: %s", filename, uploaded.get("id")
            )
            return True
        except HttpError as exc:
            logger.error("Failed to upload '%s': %s", filename, exc)
            return False


# ──────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────
def run_pipeline() -> None:
    logger.info("─── Music Discovery Pipeline starting ───")

    # ------------------------------------------------------------------
    # Load configuration from environment
    # ------------------------------------------------------------------
    lb_token = load_env("LB_TOKEN")
    lb_username = load_env("LB_USERNAME")
    folder_id = load_env("GDRIVE_FOLDER_ID")
    credentials_json = load_env("GDRIVE_CREDENTIALS")
    token_json = load_env("GDRIVE_TOKEN")

    # ------------------------------------------------------------------
    # Initialise clients
    # ------------------------------------------------------------------
    lb_client = ListenBrainzClient(token=lb_token, username=lb_username)
    drive_uploader = DriveUploader(
        credentials_json=credentials_json,
        token_json=token_json,
        folder_id=folder_id,
    )

    # ------------------------------------------------------------------
    # Phase 1 – Fetch recommendations
    # ------------------------------------------------------------------
    logger.info("Phase 1 – Fetching ListenBrainz recommendations…")
    tracks = lb_client.fetch_recommendations(count=25)
    if not tracks:
        logger.info("No tracks to process. Exiting cleanly.")
        return

    logger.info("Retrieved %d track(s) to process.", len(tracks))

    # ------------------------------------------------------------------
    # Process each track
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_dir = Path(tmp_dir)
        downloader = MusicDownloader(output_dir=output_dir)

        for track_info in tracks:
            artist = track_info["artist_name"]
            track = track_info["track_name"]
            filename = sanitize_filename(f"{artist} - {track}") + ".mp3"

            # Phase 3a – Idempotency check before download
            if drive_uploader.file_exists(filename):
                logger.info("'%s' already on Drive – skipping.", filename)
                continue

            # Phase 2 – Download
            mp3_path = downloader.download(artist=artist, track=track)
            if mp3_path is None:
                logger.warning("Skipping '%s' due to download failure.", filename)
                continue

            # Phase 3b – Upload
            success = drive_uploader.upload(mp3_path)

            # Cleanup – always delete local file to keep runner tidy
            if mp3_path.exists():
                mp3_path.unlink()
                logger.debug("Deleted local file: %s", mp3_path)

            if not success:
                logger.error("Upload failed for '%s'.", filename)

    logger.info("─── Pipeline complete ───")


if __name__ == "__main__":
    run_pipeline()
