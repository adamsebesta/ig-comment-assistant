# One image, three entrypoints: the web UI (default CMD) plus the ingest and
# worker CronJobs, which override `command`. One image means one build and
# one version tag across the whole pipeline.
# No --platform pin (unlike faceapp): nothing here is GPU- or arch-specific,
# so it builds natively on Apple Silicon for kind. Add `--platform linux/amd64`
# when building for EKS if the nodes are x86.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ica ./ica

# Non-root: nothing here needs root, and it's one less thing to explain in a
# security review.
RUN useradd --uid 10001 app
USER 10001

EXPOSE 8000
CMD ["uvicorn", "ica.web:app", "--host", "0.0.0.0", "--port", "8000"]
