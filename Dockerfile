# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.13-slim

WORKDIR /app

# gosu : permet à l'entrypoint (root) de fixer les permissions du volume
# puis de relancer le process en tant qu'appuser sans shell intermédiaire
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# Copie des dépendances installées
COPY --from=builder /install /usr/local

# Copie du code source et de l'entrypoint
COPY bot.py config.py main.py steam_client.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Répertoire pour les fichiers persistants (session, logs)
RUN mkdir /data
VOLUME ["/data"]
ENV DATA_DIR=/data

# Sortie non bufferisée pour les logs Docker
ENV PYTHONUNBUFFERED=1

# Utilisateur non-privilégié (sécurité : ne pas tourner en root)
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app

# L'entrypoint tourne d'abord en root pour chown /data, puis exec gosu appuser
ENTRYPOINT ["docker-entrypoint.sh"]
