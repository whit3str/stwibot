"""
Point d'entrée du bot Twitch → Steam Key Grabber.

Usage :
    1. Copier .env.example en .env et remplir toutes les variables.
    2. Installer les dépendances : pip install -r requirements.txt
    3. Lancer : python main.py
"""

import asyncio
import logging
import sys

from config import Config
from steam_client import SteamClient
from bot import TwitchSteamBot

# ── Configuration du logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            __import__("os").path.join(
                __import__("os").environ.get("DATA_DIR", "."), "bot.log"
            ),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)

# ── Main ────────────────────────────────────────────────────────────────────


async def main() -> None:
    config = Config()

    # Connexion Steam (bloquante, gère la 2FA TOTP automatiquement)
    logger.info("Connexion à Steam…")
    steam = SteamClient(config)
    await asyncio.to_thread(steam.login)

    # Démarrage du bot Twitch
    logger.info("Démarrage du bot Twitch…")
    bot = TwitchSteamBot(config, steam)
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
