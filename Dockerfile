# Runtime image for the hosted demo and for local development.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/

# The reference corpus, when the operator has chosen to bake one in.
#
# `data/` always exists in the repository but holds only a .keep and a README;
# `data/corpus/` is git-ignored, so client drawings never reach the public
# repository whatever anyone runs. Copying a corpus into the image is a
# deliberate act: it puts client PDFs inside a container image, and while the
# ECR repository is private, an image is easier to hand to someone than a
# folder is. Leave it empty and the hosted app serves the seeded walkthrough
# only, which is the safe default rather than an accident.
COPY data/ ./data/

ENV PYTHONPATH=/app/src
# Where the app looks for <project>/Input*.pdf. Override to point elsewhere.
ENV TRUSTSIGHT_CORPUS=/app/data/corpus
# Recorded runs, served read-only when the live path is unavailable. Also
# git-ignored and also a deliberate act to include: a frozen run holds
# rendered client drawings. Empty is fine — /ready warns rather than blocks.
ENV TRUSTSIGHT_FALLBACK=/app/data/fallback

EXPOSE 8000
# /ready reports what this instance can actually do: which corpus it sees,
# which routes are available, and whether any incomplete cloud handler is
# reachable. Check it before a demo, not /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

CMD ["uvicorn", "trustsight.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
