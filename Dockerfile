# Legal Helper backend image: one FastAPI web service (no worker, no OCR/LibreOffice — .docx only).
#
# Build from the REPO ROOT (the app serves the add-in bundle and reads the playbook from repo-root
# dirs, resolved at absolute paths /word-addin, /playbook):
#   docker build -t legal-helper .
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY playbook /playbook
COPY word-addin /word-addin
USER app
EXPOSE 8000
CMD ["sh", "-c", "python -m app.db_migrate && uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
