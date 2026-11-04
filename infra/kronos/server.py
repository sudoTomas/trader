"""Kronos forecast sidecar (#144): Salamander's HTTP wrapper around Kronos-small.

Runs in its own container (``infra/kronos/Dockerfile``), never in the trader process.
The image carries Kronos's model code (MIT, ``shiyu-coder/Kronos``, cloned at a pinned
commit) and its weights, downloaded at build time at pinned Hugging Face revisions, so
the running container needs no network.

``POST /forecast`` takes the symbol's last bars and returns ``paths`` independently
sampled close paths ``horizon`` bars ahead. Each path is its own batch row with
``sample_count=1``: the library's ``sample_count`` would average the samples into one
path and lose the spread the trader scores. ``GET /health`` names the pinned model and
reports the process's peak memory. Every forecast logs its inference time.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import threading
import time
from datetime import datetime

import pandas as pd
import torch
from fastapi import FastAPI
from model import Kronos, KronosPredictor, KronosTokenizer
from pydantic import BaseModel, Field

MODEL_ID = os.environ.get("KRONOS_MODEL", "NeoQuasar/Kronos-small")
MODEL_REVISION = os.environ["KRONOS_MODEL_REVISION"]
TOKENIZER_ID = os.environ.get("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
TOKENIZER_REVISION = os.environ["KRONOS_TOKENIZER_REVISION"]
MAX_CONTEXT = 512  # Kronos-small's context window
MAX_PATHS = 128
MAX_HORIZON = 168

tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_ID, revision=TOKENIZER_REVISION)
kronos = Kronos.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
tokenizer.eval()
kronos.eval()
predictor = KronosPredictor(kronos, tokenizer, max_context=MAX_CONTEXT)
# One forecast at a time: the GPU is shared, and a queue is cheaper than two contexts.
gpu = threading.Lock()

app = FastAPI(title="kronos-sidecar")


class BarIn(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class ForecastIn(BaseModel):
    symbol: str
    interval: str = "1h"
    horizon: int = Field(ge=1, le=MAX_HORIZON)
    paths: int = Field(ge=1, le=MAX_PATHS)
    bars: list[BarIn] = Field(min_length=32)


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1024 / 1024 if sys.platform == "darwin" else peak / 1024  # bytes vs KiB


@app.get("/health")
def health() -> dict:
    return {
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "tokenizer": TOKENIZER_ID,
        "tokenizer_revision": TOKENIZER_REVISION,
        "device": str(predictor.device),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
    }


@app.post("/forecast")
def forecast(body: ForecastIn) -> dict:
    bars = body.bars[-MAX_CONTEXT:]
    frame = pd.DataFrame([b.model_dump(exclude={"ts"}) for b in bars])
    x_ts = pd.Series(pd.to_datetime([b.ts for b in bars], utc=True).tz_localize(None))
    step = pd.Timedelta(body.interval)
    y_ts = pd.Series(pd.date_range(x_ts.iloc[-1] + step, periods=body.horizon, freq=step))
    started = time.perf_counter()
    with gpu, torch.no_grad():
        preds = predictor.predict_batch(
            [frame] * body.paths,
            [x_ts] * body.paths,
            [y_ts] * body.paths,
            pred_len=body.horizon,
            sample_count=1,
            verbose=False,
        )
    inference_ms = int((time.perf_counter() - started) * 1000)
    print(
        json.dumps(
            {
                "event": "kronos_forecast",
                "symbol": body.symbol,
                "bars": len(bars),
                "horizon": body.horizon,
                "paths": body.paths,
                "inference_ms": inference_ms,
                "peak_rss_mb": round(_peak_rss_mb(), 1),
            }
        ),
        flush=True,
    )
    return {
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "inference_ms": inference_ms,
        "paths": [p["close"].astype(float).tolist() for p in preds],
    }
