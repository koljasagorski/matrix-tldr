# syntax=docker/dockerfile:1

# ---- Builder: installiert alle Abhängigkeiten in ein venv -------------------------------
# Hinweis: matrix-nio 0.26 nutzt vodozemac (fertige Wheels) statt libolm. Es sind daher
# keine Systempakete für die Verschlüsselung nötig. Build-Tools bleiben dem Builder
# vorbehalten, falls ein Paket auf einer Plattform ohne Wheel gebaut werden muss.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt pyproject.toml README.md ./
RUN pip install -r requirements.txt

COPY src ./src
RUN pip install --no-deps .

# ---- Runtime: schlankes Image ohne Build-Tools -----------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONWARNINGS="ignore:SOCKS support in urllib3" \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/tmp \
    MATRIX_STORE_PATH=/app/store

# Non-Root-User; /app/store gehört ihm, damit das benannte Volume beschreibbar initialisiert wird.
RUN groupadd --system --gid 10001 tldr \
    && useradd --system --uid 10001 --gid tldr --home-dir /tmp --shell /usr/sbin/nologin tldr \
    && mkdir -p /app/store \
    && chown -R tldr:tldr /app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER tldr

CMD ["python", "-m", "matrix_tldr"]
