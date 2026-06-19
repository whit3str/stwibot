import base64
import json
import logging
import threading
import time
from http import HTTPStatus
from typing import Optional

import requests
from rsa import PublicKey, encrypt as _rsa_encrypt
from steampy.client import SteamClient as _SteamClient

from config import Config

logger = logging.getLogger(__name__)

ACTIVATE_KEY_URL = "https://store.steampowered.com/account/ajaxregisterkey/"

# Délai minimal (en secondes) entre deux activations de clé pour éviter le
# code 53 de Steam (« Trop de tentatives ») lorsque plusieurs clés arrivent
# simultanément.
ACTIVATE_THROTTLE_SECONDS = 3.0

# Codes retournés par Steam lors de l'activation d'une clé
REDEEM_STATUS = {
    0:  "Succès",
    9:  "Produit déjà activé",
    13: "Région non supportée",
    14: "Produit déjà activé sur un autre compte",
    15: "Clé invalide",
    24: "Clé invalide",
    36: "Jeu déjà dans la bibliothèque",
    50: "Clé non trouvée",
    53: "Trop de tentatives, réessayez plus tard",
    67: "Clé déjà activée sur ce compte",
}

import os as _os

_DATA_DIR    = _os.environ.get("DATA_DIR", ".")
LOG_FILE     = _os.path.join(_DATA_DIR, "keys_log.txt")
SESSION_FILE = _os.path.join(_DATA_DIR, "steam_session.json")


class SteamClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Optional[_SteamClient] = None
        # Jeton de rafraîchissement Steam (JWT « nonce », valide ~200 jours).
        # Permet de régénérer une session web sans redemander le code Steam Guard.
        self._refresh_token: str = ""
        self._steamid: str = ""
        # Sérialise les activations (appelées depuis des threads asyncio) et
        # applique un throttling pour rester sous les limites de Steam.
        self._activate_lock = threading.Lock()
        self._last_activation: float = 0.0

    # ------------------------------------------------------------------
    # Persistance de session
    # ------------------------------------------------------------------

    def _save_session(self) -> None:
        """Sauvegarde les cookies + steam_guard dans steam_session.json."""
        session = self._client._session
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in session.cookies
        ]
        data = {
            "cookies": cookies,
            "steam_guard": self._client.steam_guard,
            "refresh_token": self._refresh_token,
            "steamid": self._steamid or self._client.steam_guard.get("steamid", ""),
        }
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Session Steam sauvegardée dans %s", SESSION_FILE)

    def _load_session(self) -> bool:
        """
        Tente de restaurer une session précédente.
        Retourne True si la session est encore valide (pas besoin de re-login).
        """
        try:
            with open(SESSION_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return False

        self._client = _SteamClient("")
        session = self._client._session

        for c in data.get("cookies", []):
            session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])

        self._client.steam_guard = data.get("steam_guard", {})
        self._client.was_login_executed = True
        self._refresh_token = data.get("refresh_token", "")
        self._steamid = data.get("steamid", "") or self._client.steam_guard.get("steamid", "")

        # Vérification rapide : les cookies sont-ils encore valides ?
        if self._session_is_valid():
            logger.info("Session Steam restaurée depuis %s", SESSION_FILE)
            self._restore_access_token()
            return True

        # Cookies expirés → tentative de rafraîchissement silencieux via le
        # refresh_token (aucun code Steam Guard requis).
        if self._refresh_token:
            logger.info("Cookies expirés — tentative de rafraîchissement via le refresh_token…")
            try:
                if self._refresh_web_session() and self._session_is_valid():
                    logger.info("Session rafraîchie automatiquement (sans code Steam Guard).")
                    self._restore_access_token()
                    self._save_session()
                    return True
            except requests.RequestException as exc:
                logger.warning("Échec réseau lors du rafraîchissement automatique : %s", exc)
            except Exception as exc:  # noqa: BLE001 - on retombe sur le login manuel
                logger.warning("Rafraîchissement automatique impossible : %s", exc)

        logger.info("Session expirée, re-authentification requise.")
        return False

    # ------------------------------------------------------------------
    # Helpers de session
    # ------------------------------------------------------------------

    def _session_is_valid(self) -> bool:
        """Vérifie que la session web est active (pas de redirection vers /login)."""
        try:
            r = self._client._session.get(
                "https://steamcommunity.com/my/", allow_redirects=False, timeout=10
            )
            location = r.headers.get("Location", "")
            if r.status_code in (301, 302) and "login" in location:
                return False
            return True
        except requests.RequestException as exc:
            logger.warning("Impossible de vérifier la session : %s", exc)
            return False

    def _restore_access_token(self) -> None:
        """Récupère l'access_token interne de steampy (best effort)."""
        try:
            self._client._access_token = self._client._set_access_token()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossible de récupérer l'access_token steampy : %s", exc)

    def _finalize_web_login(self, refresh_token: str, steamid: str) -> bool:
        """Échange un refresh_token contre des cookies web (steamLoginSecure).

        Réutilisé à la fois par le login manuel et par le rafraîchissement
        automatique. Retourne True si les cookies ont bien été posés.
        """
        session = self._client._session
        COMMUNITY = "https://steamcommunity.com"
        STORE     = "https://store.steampowered.com"
        LOGIN     = "https://login.steampowered.com"

        # Cookie sessionid frais (sert de jeton CSRF pour finalizelogin)
        session.get(f"{STORE}/login/", headers={"Referer": STORE})
        sessionid = session.cookies.get("sessionid", "")

        r = session.post(
            f"{LOGIN}/jwt/finalizelogin",
            data={
                "nonce":     refresh_token,
                "sessionid": sessionid,
                "redir":     f"{COMMUNITY}/login/home/?goto=",
            },
            headers={"Referer": f"{STORE}/login/", "Origin": STORE},
        )
        logger.debug("finalizelogin → HTTP %d : %r", r.status_code, r.text[:300] if r.text else "(vide)")

        if r.status_code == HTTPStatus.FORBIDDEN:
            raise Exception(
                "Steam a bloqué la connexion depuis cette machine (HTTP 403).\n"
                "Connectez-vous UNE FOIS manuellement via le client Steam Desktop ou "
                "le site store.steampowered.com depuis ce PC, puis relancez le bot."
            )
        if not r.ok or not r.text:
            return False

        rdata = r.json()
        transfer = rdata.get("transfer_info", [])
        if not transfer:
            # refresh_token expiré ou invalide
            return False
        for item in transfer:
            p = {**item["params"], "steamID": rdata.get("steamID", steamid)}
            session.post(item["url"], data=p)
        return True

    def _refresh_web_session(self) -> bool:
        """Régénère une session web à partir du refresh_token stocké."""
        if not self._refresh_token or not self._steamid:
            return False
        return self._finalize_web_login(self._refresh_token, self._steamid)

    # ------------------------------------------------------------------
    # Authentification
    # ------------------------------------------------------------------

    def login(self) -> None:
        """Se connecte à Steam.

        Tente d'abord de restaurer une session existante.
        Si STEAM_SHARED_SECRET est renseigné, utilise le TOTP automatique.
        Sinon demande le code Steam Guard manuellement.
        """
        # Essai de restauration de session
        if self._load_session():
            return

        self._client = _SteamClient("")

        if self.config.steam_shared_secret:
            # ── Authentification automatique via shared_secret ──────────────
            steam_guard_json = json.dumps(
                {
                    "steamid": self.config.steam_id,
                    "shared_secret": self.config.steam_shared_secret,
                    "identity_secret": self.config.steam_identity_secret,
                }
            )
            self._client.login(
                username=self.config.steam_username,
                password=self.config.steam_password,
                steam_guard=steam_guard_json,
            )
        else:
            # ── Authentification manuelle : saisie du code Steam Guard ──────
            self._login_manual()

        logger.info("Connecté à Steam en tant que %s", self.config.steam_username)
        self._save_session()

    def _login_manual(self) -> None:
        """Login entièrement custom sans shared_secret — code Steam Guard saisi manuellement."""
        session = self._client._session

        COMMUNITY = "https://steamcommunity.com"
        STORE     = "https://store.steampowered.com"
        LOGIN     = "https://login.steampowered.com"
        API       = "https://api.steampowered.com"

        HDR = {"Referer": f"{COMMUNITY}/", "Origin": COMMUNITY}

        def _post(service: str, endpoint: str, params: dict) -> requests.Response:
            url = f"{API}/{service}/{endpoint}/v1"
            return session.post(url, data=params, headers=HDR)

        # 1. Visite initiale pour obtenir les cookies de session
        session.get(f"{STORE}/login/", headers={"Referer": STORE})

        # 2. Clé RSA pour chiffrer le mot de passe
        for _ in range(5):
            r = session.get(
                f"{API}/IAuthenticationService/GetPasswordRSAPublicKey/v1",
                params={"account_name": self.config.steam_username},
                headers=HDR,
            )
            d = r.json().get("response", {})
            if "publickey_mod" in d:
                rsa_key = PublicKey(int(d["publickey_mod"], 16), int(d["publickey_exp"], 16))
                rsa_ts  = d["timestamp"]
                break
            time.sleep(2)
        else:
            raise Exception("Impossible d'obtenir la clé RSA Steam après 5 tentatives")

        # 3. Chiffrement du mot de passe
        enc_pw = base64.b64encode(
            _rsa_encrypt(self.config.steam_password.encode("utf-8"), rsa_key)
        ).decode("utf-8")

        # 4. Initiation de la session d'authentification
        r = _post("IAuthenticationService", "BeginAuthSessionViaCredentials", {
            "persistence": "1",
            "encrypted_password": enc_pw,
            "account_name": self.config.steam_username,
            "encryption_timestamp": rsa_ts,
        })
        resp = r.json().get("response", {})
        if not resp:
            raise Exception("BeginAuthSessionViaCredentials : aucune réponse de Steam")

        client_id  = resp["client_id"]
        steamid    = resp["steamid"]
        request_id = resp["request_id"]

        # Détection du type de confirmation attendu (TOTP=3, email=2)
        code_type = 3
        prompt    = "Entrez votre code Steam Guard (app mobile, 5 caractères) : "
        for conf in resp.get("allowed_confirmations", []):
            ct = conf.get("confirmation_type", 0)
            if ct == 2:
                code_type = 2
                prompt    = "Entrez le code Steam Guard reçu par email : "
                break
            if ct == 3:
                code_type = 3
                break

        # 5. Saisie du code
        code = input(prompt).strip().upper()

        # 6. Envoi du code
        r = _post("IAuthenticationService", "UpdateAuthSessionWithSteamGuardCode", {
            "client_id": client_id,
            "steamid":   steamid,
            "code_type": str(code_type),
            "code":      code,
        })
        logger.debug("UpdateSteamGuardCode → HTTP %d", r.status_code)

        # 7. Polling du refresh_token (Steam peut prendre quelques secondes)
        refresh_token = ""
        for attempt in range(12):
            time.sleep(2)
            r = _post("IAuthenticationService", "PollAuthSessionStatus", {
                "client_id":  client_id,
                "request_id": request_id,
            })
            poll = r.json().get("response", {})
            refresh_token = poll.get("refresh_token", "")
            if refresh_token:
                break
            if "new_client_id" in poll:
                client_id = poll["new_client_id"]

        if not refresh_token:
            raise Exception(
                "PollAuthSessionStatus : refresh_token jamais reçu. "
                "Le code Steam Guard était peut-être incorrect ou expiré — relancez et réessayez."
            )

        # 8-9. Finalisation de la session JWT + transfert des cookies.
        #      On conserve le refresh_token pour pouvoir rafraîchir la session
        #      automatiquement plus tard, sans redemander de code Steam Guard.
        self._refresh_token = refresh_token
        self._steamid       = str(steamid)
        if not self._finalize_web_login(refresh_token, steamid):
            raise Exception("finalizelogin a échoué : aucun cookie de session reçu")

        # 10. Mise à jour de l'état interne du client steampy
        self._client.was_login_executed = True
        self._client.steam_guard = {
            "steamid":          steamid,
            "shared_secret":    "",
            "identity_secret":  self.config.steam_identity_secret or "",
        }
        try:
            self._client._access_token = self._client._set_access_token()
        except Exception as exc:
            logger.warning("Impossible de récupérer l'access_token steampy : %s", exc)

    # ------------------------------------------------------------------
    # Activation de clé
    # ------------------------------------------------------------------

    def activate_key(self, key: str) -> bool:
        """
        Tente d'activer une clé Steam.

        Retourne True si l'activation a réussi, False sinon.
        """
        if self._client is None:
            logger.error("Client Steam non initialisé — appelez login() d'abord.")
            return False

        # Sérialisation + throttling : une seule activation à la fois, espacées
        # d'au moins ACTIVATE_THROTTLE_SECONDS.
        with self._activate_lock:
            elapsed = time.monotonic() - self._last_activation
            if elapsed < ACTIVATE_THROTTLE_SECONDS:
                time.sleep(ACTIVATE_THROTTLE_SECONDS - elapsed)
            try:
                return self._activate_key_locked(key)
            finally:
                self._last_activation = time.monotonic()

    def _activate_key_locked(self, key: str) -> bool:
        """Effectue l'appel HTTP d'activation (sous verrou _activate_lock)."""
        assert self._client is not None

        # L'identifiant de session est stocké dans les cookies de la session requests
        # On force le domaine pour éviter CookieConflictError (plusieurs domaines Steam)
        session_id: str = self._client._session.cookies.get(
            "sessionid", "", domain="store.steampowered.com"
        ) or self._client._session.cookies.get("sessionid", "")

        try:
            response = self._client._session.post(
                ACTIVATE_KEY_URL,
                data={"product_key": key, "sessionid": session_id},
                timeout=15,
            )
            response.raise_for_status()
            data: dict = response.json()
        except requests.RequestException as exc:
            logger.error("[ERREUR réseau] %s : %s", key, exc)
            self._log(key, "ERREUR", str(exc))
            return False
        except ValueError:
            logger.error("[ERREUR JSON] %s : réponse non parseable", key)
            self._log(key, "ERREUR", "Réponse non-JSON")
            return False

        success: int = data.get("success", 0)
        purchase_detail: int = data.get("purchase_result_details", -1)

        if success == 1 and purchase_detail == 0:
            # Extraction du nom du jeu depuis le reçu
            receipt = data.get("purchase_receipt_info", {})
            items = receipt.get("line_items", [{}])
            game_name: str = items[0].get("package_description", "Jeu inconnu") if items else "Jeu inconnu"
            logger.info("[SUCCÈS] %s → %s", key, game_name)
            self._log(key, "SUCCÈS", game_name)
            self._notify_discord(key, game_name)
            return True

        error_msg = REDEEM_STATUS.get(purchase_detail, f"Code inconnu ({purchase_detail})")
        logger.warning("[ÉCHEC] %s : %s", key, error_msg)
        self._log(key, "ÉCHEC", error_msg)
        return False

    # ------------------------------------------------------------------
    # Notifications Discord
    # ------------------------------------------------------------------

    def _notify_discord(self, key: str, game_name: str) -> None:
        url = self.config.discord_webhook_url
        if not url:
            return
        payload = {
            "embeds": [{
                "title": "\u2705 Clé Steam activée avec succès",
                "color": 0x1DB954,
                "fields": [
                    {"name": "Jeu",  "value": game_name, "inline": True},
                    {"name": "Clé",   "value": f"||{key}||",  "inline": True},
                ],
                "footer": {"text": time.strftime("%Y-%m-%d %H:%M:%S")},
            }]
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            r.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Notification Discord échouée : %s", exc)

    # ------------------------------------------------------------------
    # Journalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _log(key: str, status: str, details: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] [{status}] {key} — {details}\n")
