# syntax=docker/dockerfile:1
# Base image pinned by digest; Dependabot (docker ecosystem) proposes updates.
FROM python:3.13-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d
RUN useradd --system --uid 10001 app
WORKDIR /app
COPY src/pricing ./pricing
USER 10001
ENTRYPOINT ["python", "-m", "pricing"]
