# Kronos forecast sidecar (#144). Built and run by the operator, never by CI:
#   docker compose -f infra/docker-compose.yml --profile kronos up -d --build kronos
# Runbook: docs/runbooks/shadow-signals.md.
#
# BASE_IMAGE must carry a CUDA build of PyTorch for the GPU box (an NVIDIA PyTorch
# image, e.g. nvcr.io/nvidia/pytorch:<tag>-py3, which ships torch for arm64 and amd64).
# The default slim image installs CPU torch from PyPI: fine for a smoke test, slow live.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# Pins: Kronos's model code by commit, its weights by Hugging Face revision. Moving a pin
# is a deliberate rebuild; the trader journals the revision with every forecast.
ARG KRONOS_COMMIT=67b630e67f6a18c9e9be918d9b4337c960db1e9a
ARG KRONOS_MODEL_REVISION=901c26c1332695a2a8f243eb2f37243a37bea320
ARG KRONOS_TOKENIZER_REVISION=0e0117387f39004a9016484a186a908917e22426

RUN if ! command -v git >/dev/null; then \
        apt-get update && apt-get install -y --no-install-recommends git \
        && rm -rf /var/lib/apt/lists/*; \
    fi \
    && git clone https://github.com/shiyu-coder/Kronos.git /opt/kronos \
    && git -C /opt/kronos checkout "${KRONOS_COMMIT}"

# torch only when the base image lacks it (the NVIDIA images ship a CUDA build).
RUN python -c "import torch" 2>/dev/null || pip install --no-cache-dir torch \
    && pip install --no-cache-dir einops==0.8.1 huggingface_hub==0.33.1 safetensors==0.6.2 \
        pandas "fastapi>=0.115" "uvicorn>=0.32"

ENV PYTHONPATH=/opt/kronos \
    HF_HOME=/opt/hf \
    KRONOS_MODEL_REVISION=${KRONOS_MODEL_REVISION} \
    KRONOS_TOKENIZER_REVISION=${KRONOS_TOKENIZER_REVISION}

# Weights into the image at the pinned revisions; the container then runs offline.
RUN python -c "from huggingface_hub import snapshot_download as d; \
d('NeoQuasar/Kronos-small', revision='${KRONOS_MODEL_REVISION}'); \
d('NeoQuasar/Kronos-Tokenizer-base', revision='${KRONOS_TOKENIZER_REVISION}')"
ENV HF_HUB_OFFLINE=1

COPY server.py /opt/sidecar/server.py
WORKDIR /opt/sidecar
EXPOSE 8095
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8095"]
