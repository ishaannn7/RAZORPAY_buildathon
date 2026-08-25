# ReconProof API image.
#
# Mirrors what `make setup` does — install the package with its declared
# dependencies — and what `make api` runs — `reconproof serve`. No compiler
# toolchain is required: numpy/scipy/scikit-learn/polars all ship manylinux
# wheels for this base image's platform.
FROM python:3.12-slim

WORKDIR /app

# Dependencies first so an application-code change doesn't invalidate this
# layer.
COPY pyproject.toml ./
COPY reconproof ./reconproof
RUN pip install --no-cache-dir .

# Runtime state (SQLite database, uploads, model/eval artifacts) lives here.
# Mount a volume at this path to persist it across container restarts — see
# docker-compose.yml.
ENV RECONPROOF_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8817

CMD ["reconproof", "serve", "--host", "0.0.0.0", "--port", "8817"]
