# syntax=docker/dockerfile:1
# Base image pinned by digest; Dependabot (docker ecosystem) proposes updates.
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9
RUN useradd --system --uid 10001 app
WORKDIR /app
COPY src/pricing ./pricing
USER 10001
ENTRYPOINT ["python", "-m", "pricing"]
