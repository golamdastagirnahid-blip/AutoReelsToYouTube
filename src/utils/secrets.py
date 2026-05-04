"""Safe loader for API keys and secrets.

Loads from environment variables (set by GitHub Actions)
or from a local .env file (gitignored) for development.

NEVER hardcode secrets. NEVER log full key values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env if present (local dev). In GitHub Actions, env is set by Secrets.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=False)


def _get(name: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(
            f"Missing required secret: {name}. "
            f"Set it in .env (local) or GitHub Secrets (production)."
        )
    return val


def _mask(val: Optional[str]) -> str:
    if not val:
        return "<missing>"
    if len(val) <= 8:
        return "***"
    return f"{val[:4]}...{val[-4:]}"


@dataclass(frozen=True)
class Secrets:
    nvidia_api_key: str
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    youtube_client_id: str
    youtube_client_secret: str
    youtube_refresh_token: Optional[str]
    pexels_api_key: Optional[str]
    pixabay_api_key: Optional[str]
    unsplash_access_key: Optional[str]
    jamendo_client_id: Optional[str]
    instagram_username: Optional[str]
    instagram_password: Optional[str]
    owner_contact_email: str
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]

    @classmethod
    def load(cls) -> "Secrets":
        return cls(
            nvidia_api_key=_get("NVIDIA_API_KEY"),
            elevenlabs_api_key=_get("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=_get("ELEVENLABS_VOICE_ID", default="pNInz6obpgDQGcFmaJgB"),
            youtube_client_id=_get("YOUTUBE_CLIENT_ID"),
            youtube_client_secret=_get("YOUTUBE_CLIENT_SECRET"),
            youtube_refresh_token=_get("YOUTUBE_REFRESH_TOKEN", required=False),
            pexels_api_key=_get("PEXELS_API_KEY", required=False),
            pixabay_api_key=_get("PIXABAY_API_KEY", required=False),
            unsplash_access_key=_get("UNSPLASH_ACCESS_KEY", required=False),
            jamendo_client_id=_get("JAMENDO_CLIENT_ID", required=False),
            instagram_username=_get("INSTAGRAM_USERNAME", required=False),
            instagram_password=_get("INSTAGRAM_PASSWORD", required=False),
            owner_contact_email=_get("OWNER_CONTACT_EMAIL", default="contact@example.com"),
            telegram_bot_token=_get("TELEGRAM_BOT_TOKEN", required=False),
            telegram_chat_id=_get("TELEGRAM_CHAT_ID", required=False),
        )

    def summary(self) -> str:
        """Safe-to-log summary (masked keys)."""
        return (
            f"Secrets loaded:\n"
            f"  NVIDIA       : {_mask(self.nvidia_api_key)}\n"
            f"  ElevenLabs   : {_mask(self.elevenlabs_api_key)}\n"
            f"  YouTube CID  : {_mask(self.youtube_client_id)}\n"
            f"  YouTube TOK  : {_mask(self.youtube_refresh_token)}\n"
            f"  Pexels       : {_mask(self.pexels_api_key)}\n"
            f"  Pixabay      : {_mask(self.pixabay_api_key)}\n"
            f"  Unsplash     : {_mask(self.unsplash_access_key)}\n"
            f"  Jamendo      : {_mask(self.jamendo_client_id)}\n"
            f"  Owner email  : {self.owner_contact_email}"
        )


if __name__ == "__main__":
    print(Secrets.load().summary())
