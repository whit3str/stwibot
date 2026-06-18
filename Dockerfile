# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.13-slim

WORKDIR /app

# Copie des dépendances installées
COPY --from=builder /install /usr/local

# Copie du code source
COPY bot.py config.py main.py steam_client.py ./

# Répertoire pour les fichiers persistants (session, logs)
RUN mkdir /data
VOLUME ["/data"]
ENV DATA_DIR=/data

# Sortie non bufferisée pour les logs Docker
ENV PYTHONUNBUFFERED=1

# Utilisateur non-privilégié (sécurité : ne pas tourner en root)
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app /data
USER appuser

ENTRYPOINT ["python", "main.py"]
