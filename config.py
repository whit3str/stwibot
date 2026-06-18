import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Twitch
    twitch_token: str = os.environ["TWITCH_TOKEN"]
    twitch_channel: str = os.getenv("TWITCH_CHANNEL", "mistermv")
    twitch_bot_nick: str = os.environ["TWITCH_BOT_NICK"]

    _allowed_raw: str = os.getenv("TWITCH_ALLOWED_USERS", "")
    allowed_users: set[str] = (
        {u.strip().lower() for u in _allowed_raw.split(",") if u.strip()}
        if _allowed_raw.strip()
        else set()
    )

    # Discord
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    # Steam
    steam_username: str = os.environ["STEAM_USERNAME"]
    steam_password: str = os.environ["STEAM_PASSWORD"]
    # Secrets 2FA optionnels : si absents, le code Steam Guard sera demandé manuellement
    steam_shared_secret: str = os.getenv("STEAM_SHARED_SECRET", "").strip()
    steam_identity_secret: str = os.getenv("STEAM_IDENTITY_SECRET", "").strip()
    steam_id: str = os.getenv("STEAM_ID", "")
