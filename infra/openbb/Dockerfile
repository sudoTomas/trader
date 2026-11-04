# OpenBB sidecar image (#141): `openbb-api` on :6900 with the pinned routers and keyless
# providers in requirements.txt. Built by the `openbb` compose service (profile `openbb`,
# so a plain `up -d` never starts it). Runbook: docs/runbooks/deploy.md.
FROM python:3.12-slim-bookworm

RUN useradd --create-home openbb
WORKDIR /home/openbb
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

USER openbb
EXPOSE 6900
# 0.0.0.0 inside the container only; compose publishes it on the host's loopback.
CMD ["openbb-api", "--host", "0.0.0.0", "--port", "6900"]
