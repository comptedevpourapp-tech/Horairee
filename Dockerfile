# Image légère pour un usage type TrueNAS SCALE (Apps personnalisées) ou Docker pur
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Brussels \
    HP_HOST=0.0.0.0 \
    HP_ARCHIVE=/app/data/archive_horaires.json

ARG GIT_SHA=inconnue
ENV HP_VERSION=${GIT_SHA}

WORKDIR /app

COPY horaires.py serveur_horaires.py page_horaires.html liens_annuels.json liens_groupes.json ./

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && useradd --system --uid 1000 --no-create-home horaire \
 && mkdir -p /app/data \
 && chown -R horaire:horaire /app
USER horaire

EXPOSE 8000

HEALTHCHECK --interval=5m --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('HP_PORT','8000')+'/',timeout=4)"

CMD ["python", "serveur_horaires.py"]
