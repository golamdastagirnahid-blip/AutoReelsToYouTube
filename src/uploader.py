"""Stage 7 — Upload the finished video to YouTube as a Short."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)


def _build_credentials(client_id: str, client_secret: str, refresh_token: str) -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube",
        ],
    )
    creds.refresh(Request())
    return creds


def upload_short(
    video_path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    client_id: str,
    client_secret: str,
    refresh_token: str,
    privacy: str = "public",      # 'public' | 'unlisted' | 'private'
    category_id: str = "22",      # 22 = People & Blogs
    made_for_kids: bool = False,
) -> Optional[str]:
    """Upload and return the YouTube video ID, or None on failure."""
    if not video_path.exists():
        log.error("Video not found: %s", video_path)
        return None

    creds = _build_credentials(client_id, client_secret, refresh_token)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    # Ensure #Shorts is in description for Shorts shelf eligibility
    if "#shorts" not in description.lower():
        description = description.rstrip() + "\n\n#Shorts"

    body = {
        "snippet": {
            "title": title[:95],
            "description": description[:4900],
            "tags": tags[:25],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
            "embeddable": True,
        },
    }

    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    try:
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                log.info("Upload progress: %d%%", int(status.progress() * 100))
        vid = response.get("id")
        log.info("Uploaded: https://youtube.com/shorts/%s", vid)
        return vid
    except Exception as e:
        log.error("YouTube upload failed: %s", e)
        return None
