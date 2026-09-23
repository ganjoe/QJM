FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Als Paket "server", damit die relativen Importe in app.py greifen.
COPY requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt
COPY . /app/server/

# Der Client-Code wird als Volume gemountet (/app/client) und ueber /api/sync
# an die Arbeitsrechner verteilt. schedules.py kommt aus der Sekretaerin — eine
# Quelle fuer die Zeitregeln, kein zweiter Satz Formulierungen.
EXPOSE 8799

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8799", "--log-level", "info"]
