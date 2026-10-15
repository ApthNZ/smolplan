# Pinned by digest as well as tag, so a rebuild is the image that was tested and
# not whatever the tag points at today. Dependabot's docker ecosystem moves the
# digest forward, and the automerge workflow builds and health-checks the image
# before merging it.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Every module, not a list of names — a new module must not be able to be
# forgotten here and crash the container on import.
COPY *.py ./
COPY static/ ./static/

# The database lives on a volume so the image stays disposable.
ENV SMOLPLAN_DB=/data/smolplan.db
RUN mkdir -p /data \
    && useradd -u 10001 -r smolplan \
    && chown smolplan /data \
    && chmod -R a+rX /app
USER smolplan
VOLUME /data

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
