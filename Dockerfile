# Image du backend (production Linux).
#
# WeasyPrint (PDF/A des documents archivés) a besoin de Pango : bibliothèques SYSTÈME, installées ici
# par apt (libpango + libpangoft2 ; libpangocairo par précaution selon la version de WeasyPrint).
# Sans elles, l'application démarre mais chaque génération de document échoue (statut « echec »
# à l'index) — voir docs/runbook-documents.md.
ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Versions figées : aucune plage ouverte en production (CLAUDE §16).
COPY requirements-lock.txt .
RUN pip install --no-cache-dir -r requirements-lock.txt

COPY . .

# Exécution en utilisateur non privilégié. Le dossier d'archive est un VOLUME : il doit survivre au
# conteneur, être sauvegardé, et n'est jamais nettoyé (conservation longue des pièces légales).
RUN useradd --system --uid 10001 --no-create-home mifin \
    && mkdir -p /data/documents \
    && chown mifin /data/documents
USER mifin
# XDG_CACHE_HOME : fontconfig (via Pango) veut un cache inscriptible ; l'utilisateur système n'a pas
# de dossier personnel, sans cela chaque rendu écrit des avertissements.
ENV ENV=production \
    DOCUMENTS_REPERTOIRE=/data/documents \
    XDG_CACHE_HOME=/tmp/cache
VOLUME ["/data/documents"]

EXPOSE 8001

# « Prêt » (et non seulement « vivant ») : le dossier d'archive existe et est inscriptible.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health/ready').status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
