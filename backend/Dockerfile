# NDA Assistant backend image. One image, two roles:
#   * nda-api    (default command below): the FastAPI app via uvicorn's factory pattern.
#   * nda-worker (command override): `python -m app.worker` — same image, worker entrypoint.
#
# Build from the REPO ROOT (the engine reads its playbook + samples data and serves the add-in bundle
# from repo-root dirs, which the app resolves at the absolute paths /playbook, /samples, /word-addin —
# _REPO = Path(__file__).parents[3] == "/" in this image). Context is the repo root; .dockerignore
# (repo root) keeps it small:
#   docker build -f backend/Dockerfile -t nda-backend .
#   az acr build --registry <acr> --image nda-assistant:<tag> --file backend/Dockerfile .

FROM python:3.13-slim

# Fail fast, no .pyc, unbuffered stdout so container logs stream in real time.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Non-root runtime user (created before we drop to it below).
RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

# System packages the engine needs at runtime (P1 ingestion + P4 conversion):
#   * tesseract-ocr — the OCR binary behind pytesseract (scanned-PDF review path; OCR_ENABLED
#     defaults true). The stock eng.traineddata is replaced with tessdata_best (same choice as the
#     old engine image) for meaningfully better accuracy on degraded legal scans.
#   * libreoffice-writer — the `soffice` binary (SOFFICE_BIN) for .doc→.docx ingestion conversion
#     and the archive docx→PDF path (PLAN §2: soffice replaces Gotenberg; one conversion engine).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr libreoffice-writer ca-certificates curl \
    && curl -fsSL -o /usr/share/tesseract-ocr/5/tessdata/eng.traineddata \
         https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so the layer caches across code changes (context = repo root).
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

# Application code (tests/tooling configs are excluded via .dockerignore).
COPY backend/app ./app

# Engine data + add-in bundle from the repo root, at the ABSOLUTE paths the app resolves
# (_REPO == "/"): the review playbook (v3 json + v4 tree), the standard-NDA samples, and the Word
# add-in static bundle (node_modules excluded via .dockerignore). Missing any of these is a
# fail-closed 503 (playbook) or a fail-soft disable (add-in) — so ship them, don't discover at runtime.
COPY playbook /playbook
COPY samples /samples
COPY word-addin /word-addin

# The runtime binaries the code shells out to must exist — fail the BUILD, not a 3am review.
RUN tesseract --version && soffice --version \
    && test -f /playbook/playbook_nda_v3.json && test -f /playbook/v4/manifest.json \
    && test -d /samples && test -f /word-addin/taskpane.html

USER app
EXPOSE 8000

# nda-api (default): uvicorn calls the create_app() factory.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# nda-worker: override the command in the Container App definition with:
#   python -m app.worker
