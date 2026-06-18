import asyncio
import logging
import re
from collections import deque

import twitchio
from twitchio.ext import commands

from config import Config
from steam_client import SteamClient

logger = logging.getLogger(__name__)

# Regex : format standard Steam XXXXX-XXXXX-XXXXX (alphanumérique majuscule)
# Les clés Steam sont toujours en majuscules et composées de chiffres + lettres
_KEY_RE = re.compile(r"\b([A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5})\b")

# Nombre maximum de clés mémorisées pour l'anti-doublon (évite la fuite mémoire
# sur un stream de plusieurs heures).
_MAX_SEEN_KEYS = 5000


class TwitchSteamBot(commands.Bot):
    def __init__(self, config: Config, steam: SteamClient) -> None:
        super().__init__(
            token=config.twitch_token,
            prefix="!",
            initial_channels=[config.twitch_channel],
        )
        self.config = config
        self.steam = steam
        # Anti-doublon borné : un set pour la recherche O(1) + une deque pour
        # purger les plus anciennes clés quand la limite est atteinte.
        self._seen_keys: set[str] = set()
        self._seen_order: deque[str] = deque()

    # ------------------------------------------------------------------
    # Événements TwitchIO
    # ------------------------------------------------------------------

    async def event_ready(self) -> None:
        logger.info("Bot connecté en tant que %s", self.nick)
        logger.info(
            "Surveillance du canal : #%s | Utilisateurs filtrés : %s",
            self.config.twitch_channel,
            self.config.allowed_users if self.config.allowed_users else "tous",
        )

    async def event_message(self, message: twitchio.Message) -> None:
        # Ignorer les messages envoyés par le bot lui-même
        if message.echo:
            return

        author_name: str = message.author.name.lower()

        # Filtre par utilisateurs si la whitelist est définie
        if self.config.allowed_users and author_name not in self.config.allowed_users:
            return

        # Chercher des clés Steam dans le message (converti en majuscules)
        keys = _KEY_RE.findall(message.content.upper())
        for key in keys:
            if key in self._seen_keys:
                logger.debug("Clé déjà traitée, ignorée : %s", key)
                continue

            self._remember_key(key)
            logger.info(
                "Clé détectée dans le message de %s : %s", message.author.name, key
            )
            # Activation déportée dans un thread pour ne pas bloquer la boucle
            # asyncio (l'appel HTTP Steam peut prendre jusqu'à 15 s).
            asyncio.create_task(asyncio.to_thread(self.steam.activate_key, key))

    def _remember_key(self, key: str) -> None:
        """Mémorise une clé traitée en bornant la taille de l'historique."""
        self._seen_keys.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > _MAX_SEEN_KEYS:
            oldest = self._seen_order.popleft()
            self._seen_keys.discard(oldest)
