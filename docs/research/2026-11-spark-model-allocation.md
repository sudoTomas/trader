# DGX Spark memory allocation: which models, which runtimes, which workloads (2026-11)

**Question.** The one DGX Spark (GB10, 128 GB unified, ~121 GB usable, ~273 GB/s) serves four
workloads: Mem0 memory, the Salamander desk, OpenCode, and Hermes Agent. How should the memory be
split, and which current open-weights models fit best?

**Method.** Model facts come from Hugging Face model cards and the HF API (`/api/models/<id>`,
`config.json`), prices from OpenRouter's `GET /api/v1/models` (pulled 2026-11-11), and speeds
from DGX Spark / GB10 owners' published measurements. Each number has its source next to it.
Where owners disagree, the table gives the range. Nothing on the Spark was run or changed.

---

## 1. Recommendation

**Retire the idle Nemotron 3 Super instance. Run two small-active MoE models side by side
(Plan B), and keep Grok as the primary for the trading research roles.**

| Workload | Serve it on | Why |
|---|---|---|
| Mem0 embeddings | `qwen3-embedding:4b` on Ollama, **unchanged** | Best retrieval quality in its class (MTEB multilingual 69.45). Changing models means changing the dimension, which means re-embedding every Qdrant collection, and nothing gains enough to pay for that (§3.4). |
| Mem0 extraction | `qwen3:8b` for now. Later, optionally, the Lightning instance with thinking off by server default (§6 step 6). | Frees 6.6 GB and drops the `think:false` client wrapper. |
| Salamander `decide` + analyst/critic/reflect backstop | **Nemotron 3.5 Lightning 30B-A3B NVFP4** on vLLM at `:8009` (the preset URL, so the thinking guard keeps working) | 70–93 tok/s single-stream on GB10, against 16–24 tok/s for today's 3 Super. Same `enable_thinking` switch, NVIDIA recipe written for Spark, ~22 GB. Make it primary only after a replay gate (below). |
| Brief, compiler, sentiment, research roles | **Grok via SuperGrok** (unchanged); OpenRouter as fallback | No per-token cost, and `x_search` exists only on xAI. Local adds nothing here. |
| Confirm (Kev) and Kronos | Unchanged (`kev.serve` :8011, ~17 GB; Kronos ~1.4 GB) | Outside this decision. |
| OpenCode + Hermes Agent | **Qwen3.6-35B-A3B NVFP4** (NVIDIA mixed NVFP4/FP8 checkpoint) on vLLM, 128K context, thinking on per request | 76–108 tok/s single-stream on GB10. It is the default in NVIDIA's own Spark coding-agent playbook and the most-reported daily driver on single Sparks. |

**Why this shape:**

1. **The 88 GB block is dead weight today.** `nemotron-vllm` has served zero requests in six
   days, and it pushes the box to 120/121 GB used. That is the condition the local lesson says
   wedges the box.
2. **Bandwidth, not capacity, limits GB10.** At 273 GB/s, a 3B-active model decodes 4–5× faster
   than a 12B-active one. The decide call is short (≤512 output tokens), so speed per call beats
   raw quality there, and the desk, not the model, supplies the deliberation
   ([nemotron-thinking-off](../lessons.md#nemotron-thinking-off)).
3. **The money argument for running trading locally is weak.** 1,000 decide calls cost
   $0.14–0.60 on OpenRouter for these model classes (§5), roughly $4–18 a month. Local decide is
   worth having as an **outage backstop that never depends on xAI or OpenRouter**, not as a cost
   saving.
4. **The money argument for OpenCode and Hermes locally is real.** They burn tens of millions
   of tokens a day, and Hermes plus Grok Build draw on the same SuperGrok usage pool (§5).
   Moving them local frees that pool and removes rate-limit exposure.
5. **The two-model pairing has been measured on GB10** (Qwen3.6-35B NVFP4 + Nemotron 3.5
   Lightning, vLLM 0.28.1, in production since September), including its failure modes and
   fixes ([forum, 2026-10-21][f-twomodels]).

**Do not promote Lightning to primary decide without a replay gate.** Lightning is a 3B-active
model replacing a 12B-active one. Run `eval/replay.py` over the blind-replay window with
`TRADER_NEMOTRON_MODEL` pointed at Lightning, and compare it with the Grok baseline on schema
validity, the hold/open/close mix and the replay P&L proxy before it becomes primary. If it loses
clearly, keep Grok primary and Lightning as the backstop, which is today's arrangement with a
backstop that actually runs.

---

## 2. Plan comparison

All three plans share these **fixed tenants**:

| Fixed tenant | GB |
|---|---|
| OS, page cache, Docker, trader API + Postgres, CUDA contexts (reserve) | 10 |
| Kev-9b (`kev.serve` :8011) | 17 |
| Kronos sidecar | 1.4 |
| `qwen3-embedding:4b` (Ollama) | 4.9 |
| **Subtotal** | **33.3** |

Plan budgets follow (GB, at the stated context; KV figures are computed in §3.5).

| | **A. One large shared MoE** | **B. Two small-active MoEs (recommended)** | **C. Trading on cloud; box for coding and agents** |
|---|---|---|---|
| Model(s) | Nemotron 3.5 Super VL 120B-A12B NVFP4 + LiLiCorr drafter | Nemotron 3.5 Lightning 30B-A3B NVFP4 (decide, trading backstop) + Qwen3.6-35B-A3B NVFP4 (OpenCode, Hermes) | Nemotron 3.5 Super VL 120B-A12B NVFP4 (OpenCode, Hermes); trading on Grok/OpenRouter |
| vLLM `--gpu-memory-utilization` | 0.62 (~74 GB) | Lightning 0.22 (~26 GB) + Qwen3.6 0.30 (~36 GB) = 0.52 | 0.62 (~74 GB) |
| Weights | ~75 (repo size) | ~22 + ~22 | ~75 |
| KV / state headroom inside the pool | very little (≤2 seq × 64K) | Lightning 4 × 32K; Qwen3.6 4 × 128K | very little (1–2 seq × 64K) |
| Mem0 extraction | served by Super VL (drop `qwen3:8b`) | `qwen3:8b` 6.6 | `qwen3:8b` 6.6 |
| **Total** | 33.3 + 74 = **~107** | 33.3 + 62 + 6.6 = **~102** | 33.3 + 74 + 6.6 = **~114: over budget.** Fits only with Kev moved to FP8 or `qwen3:8b` dropped (→ ~107). |
| Free headroom (of ~121) | ~14 | **~19** | ~7 (~14 after the trim) |
| Decode, single stream (GB10) | 34–49 tok/s with LiLiCorr up to 16K; 19 without ([forum][f-supervl], [Classmethod][cm-supervl]) | Lightning 70–93; Qwen3.6 76–108 ([pi3g][pi3g], [forum][f-lightning-search], [Classmethod][cm-qwen36]) | as A |
| Concurrency | LiLiCorr recipes pin `--max-num-seqs 1`; c4 reached 82 tok/s aggregate and also OOMed ([forum][f-boltsuper]) | 8-concurrent: 263.7 / 293.5 tok/s per model alone, **107.7 / 149.6 when both run at once** ([forum][f-twomodels]) | as A |
| Prefill | 2,100–3,000 tok/s ([forum][f-supervl]) | ~6,000 tok/s on 3B-active models ([forum][f-measured]) | as A |
| Quality ceiling | highest: SWE-bench Verified 71.1, Terminal-Bench 2.1 62.8 (NVFP4, [card][hf-supervl]) | middle: Lightning SWE-bench Verified 52.8 ([card][hf-lightning]); Qwen3.6-35B-A3B a step below Bolt and Super VL on agentic benchmarks ([Bolt card][hf-bolt]) | highest |
| What breaks | **One queue for everything.** An OpenCode 60K-token prefill blocks decide for ~20–30 s. **Thermal power-offs at 32K+ prefill** reported on this exact model; a 2,200 MHz clock cap plus a fan stopped them ([forum][f-supervl]). 14 GB headroom with a 75 GB model is under the margin owners use (they swap at ~10 GB free, [repo][gh-q38]). | Shared bandwidth: under joint load each model gets about half. A cuDNN FP8 GEMM crash on GB10 needs a one-line FlashInfer backend patch. vLLM exits 0 on `EngineDeadError`, so units need `Restart=always` ([forum][f-twomodels]). | Same thermal and queue risks as A, but the money path never waits on it. The box no longer backs up trading: an xAI plus OpenRouter outage stops decide. |
| Fit for the four workloads | Mem0 ✔, trading ✔ (slow and contended), OpenCode ✔ (best quality, worst latency), Hermes ✔ | Mem0 ✔, trading ✔ (fast, isolated instance), OpenCode ✔ (good, fast), Hermes ✔ | Mem0 ✔, trading ✘ local, OpenCode ✔✔, Hermes ✔✔ |

**Variants worth knowing:**

- **B′: Qwen3-Coder-Next 80B-A3B (4-bit) instead of Qwen3.6-35B for OpenCode.** 74.8 tok/s
  single and 289 tok/s at 8× on vLLM ([forum][f-measured]), but roughly 45 GB of weights. That
  fits only if `qwen3:8b` moves to Lightning (total ~105 GB). It is older (Feb 2026) and
  coding-only, which makes it worse for Hermes.
- **B″: Nemotron 3.5 Bolt 74B-A9B "24GB" instead of Lightning.** A Super VL derivative packed to
  2-bit experts, with 27.9–31.4 GiB resident. It runs 39 tok/s on llama.cpp and 62 with DFlash
  ([Classmethod][cm-bolt]); another owner measured 70.8 tok/s at c1 and 9.9 at c4 with 32K depth
  ([forum][f-boltsuper]). Vendor agentic scores beat Qwen3.6-35B ([card][hf-bolt]). The tester,
  however, found thinking budgets exhausted with no answer and judged it "challenging" for
  OpenCode and Hermes. It needs a nightly vLLM plus the `b12x` kernel. Worth a later replay
  trial; too new to anchor on.

---

## 3. Candidate models that fit

### 3.1 General and agentic (Hermes, trading roles)

| Model | Total / active | Fits as | Weights in memory | GB10 decode (single) | GB10 notes | Tool calls / JSON | Licence |
|---|---|---|---|---|---|---|---|
| **Nemotron 3 Super 120B-A12B** (running today) | 120B / 12B | NVFP4 | ~75 GB repo ([pi3g][pi3g]); llama.cpp Q4_K 65.1 GiB ([nabe2030][gh-nabe]) | 15.7 (vLLM 0.29, [pi3g][pi3g]); 22.7–23.7 (vLLM blog, no MTP, [vLLM][vllm-spark]); 19.5–35 (llama.cpp, [nabe2030][gh-nabe]) | Prefill ~1,880 tok/s at 2–7K ([vLLM][vllm-spark]). Needs thinking off ([lesson](../lessons.md#nemotron-thinking-off)). | vLLM structured outputs; `qwen3_coder` parser | NVIDIA Open Model |
| **Nemotron 3.5 Super VL 120B-A12B** (Oct 2026) | 120B / 12B, Mamba-2/attention hybrid, MTP, vision | NVFP4 | 75.0 GB repo (HF API) | 34–49 with LiLiCorr (≤16K), 19 without ([forum][f-supervl], [Classmethod][cm-supervl]) | NVIDIA ships Spark recipes for vLLM, SGLang and TRT-LLM, all at 1–2 sequences ([card][hf-supervl]). Power-offs at 32K+ prefill. | 20/20 tool-calling tests passed ([forum][f-boltsuper]) | OpenMDW-1.1 |
| **Nemotron 3.5 Bolt 74B-A9B 24GB** (Oct 2026) | 74B / 9B | native 2-bit experts (IQ2_XXS), NVFP4 Mamba | 27.9–31.4 GiB resident ([Classmethod][cm-bolt]) | 31.5 (vLLM), 39.5 (llama.cpp), 61.6 (DFlash) ([Classmethod][cm-bolt]); 70.8 ([forum][f-boltsuper]) | Scales poorly: 13.3 tok/s per request at 8 concurrent | 5/5 basic tool calls; 4/6 picks from 40 tools; JSON format 4–5/5 ([Classmethod][cm-bolt]) | OpenMDW-1.1 |
| **Nemotron 3.5 Lightning 30B-A3B** (Aug 2026) | 30B / 3B, hybrid, MTP | NVFP4 | 21.6 GB repo (HF API) | 69.9 (vLLM 0.29, [pi3g][pi3g]); 78.5 target-only, 90.7 with DSpark ([forum search][f-lightning-search]) | Spark recipe: `--moe-backend marlin --kv-cache-dtype fp8 --mamba-backend flashinfer`, DSpark drafter ([card][hf-lightning]) | `qwen3_coder` parser; thinking off via `chat_template_kwargs` | OpenMDW-1.1 |
| **Qwen3.6-35B-A3B** (Apr 2026) | 36B / 3B, Gated DeltaNet hybrid | NVIDIA NVFP4/FP8 mix | ~22 GB ([pi3g][pi3g]) | 76.4 (vLLM 0.29, [pi3g][pi3g]); 77.9 ([forum][f-twomodels]); 108 with MTP ([Classmethod][cm-qwen36]) | One owner reports NVFP4 falling back to Marlin at 40.8 and FP8 at 53.8 ([ai-muninn][muninn-trap]). A hang after attention-backend selection was reported across three images ([forum][f-q36hang]). | Daily driver for Hermes and OpenCode on single Sparks ([forum][f-coding]) | Apache-2.0 |
| **gpt-oss-120b** | 117B / 5.1B | MXFP4 (native) | ~63 GB | 60.7 (vLLM), 42.1 (Ollama) ([forum][f-measured]); 52.9 (llama.cpp tg32, [JetsonHacks][jh]) | Mature; reasoning-only (harmony format), so thinking cannot be fully switched off | structured outputs on vLLM | Apache-2.0 |
| **Qwen3.5-122B-A10B** | 125B / 10B | int4 AutoRound / NVFP4 | ~65–70 GB | ~26 ([forum][f-coding]) | Praised for tool calling up to 256K | good, by owner reports | Apache-2.0 |
| **Qwen3.8-27B** (dense, Aug 2026) | 27.8B dense | NVFP4 ~18–22 GB | 18–25 GB | 9.5–18.5 on vLLM; 36.6 (SGLang + DSpark); 47.9 (DFlash2) ([forum][f-q38-27b]) | Dense, so bandwidth-bound; fast only with a drafter on SGLang | strong coder (SWE-bench Pro 61.7 in Qwen's table, [card][hf-q38flash]) | Apache-2.0 |
| **Qwen3.8-Flash-Next** (Aug 2026) | 125B / 6B + 51B n-gram table + 4B MTP | NVFP4 | ~76 GiB resident; the 47.7 GiB n-gram table is moved to NVMe by a **custom patch** ([repo][gh-q38]) | 17 without MTP; 32.5–43.9 patched ([repo][gh-q38], [ai-muninn][muninn-q38]) | Swaps at ~10 GB free; weights take ~10.5 min to load | tool-eval-bench 89–93/100 with thinking off ([forum][f-q38flash]) | qwen-community-1.0 |
| **Gemma 4 26B-A4B** | 25.8B / 3.8B | NVFP4 | ~15 GB | 54.9 with MTP, 30.3 without ([forum][f-measured]) | – | structured outputs | Apache-2.0 |
| **GLM-4.7-Flash** | 31B / ~3B | Q4/Q8 | 18–33 GB | not found on GB10 | One owner reports tool-call trouble on Spark ([OpenCode search][f-coding]) | – | MIT |

**Too big for one Spark next to anything else** (checked on the HF API and model cards):
GLM-5.3-Flash at 321B, DeepSeek-V4-Flash at 284B/13B (only Q2 GGUF fits, and owners say Q2 fails
at multi-step work, [forum][f-coding]), DeepSeek-V4.1-Flash at 763B, MiniMax-M3.1-Flash at
428B/23B ([card][hf-minimax]), Step-3.7-Flash at 196B (IQ3 at 22 tok/s looped, [forum][f-coding]),
the Kimi K3 family, and Ling-3.1-flash at 560B. Ling-3.0-flash (124B/5.1B, MIT) would fit at 4-bit,
but no GB10 measurements were found.

### 3.2 Coding (OpenCode)

OpenCode needs at least ~64K context and reliable tool calls ([Aliteq][aliteq]; Hermes has the
same floor, [developersdigest][hermes-guide]).

| Model | Why / why not on this box |
|---|---|
| **Qwen3.6-35B-A3B NVFP4 (pick)** | Fast (76–108 tok/s), ~22 GB, long context is cheap (10 of 40 layers hold KV). It is NVIDIA's default for OpenCode, Claude Code and Codex on Spark (`qwen3.6:35b-a3b-mtp-q4_K_M`, ~23 GB, [playbook][nv-cli]). |
| Qwen3-Coder-Next 80B-A3B | 74.8 tok/s, 289 tok/s at 8×; coding-specialised; ~45 GB. The best pure-coding choice if Hermes moves elsewhere. |
| Nemotron 3.5 Super VL | Highest quality that fits (SWE-bench Verified 71.1), but 34–49 tok/s at one sequence, plus the long-prefill thermal risk. |
| Qwen3.8-27B | Strong scores, but dense: usable only with an SGLang drafter (36–48 tok/s) and weak at concurrency. |
| Qwen3.8-Flash-Next | The strongest benchmarks (SWE-bench Pro 62.5), but on one Spark it needs a patched vLLM, takes ~76 GiB and leaves no room for Kev. |

### 3.3 Small and fast (decide, extraction)

| Model | Size | GB10 decode | Notes |
|---|---|---|---|
| **Nemotron 3.5 Lightning NVFP4** | ~22 GB | 70–93 | Pick. Same family and the same thinking switch as the current preset. |
| Qwen3.6-35B-A3B NVFP4 | ~22 GB | 76–108 | Equal choice; already in Plan B for coding. |
| `qwen3:8b` (Ollama, current Mem0 extractor) | 6.6 GB | – | Works only with `think:false` forced. Ollama does not batch: its 8× throughput equals its single-stream throughput ([forum][f-measured]). |
| Granite 4.2 8B, Ministral 8B | ~8–16 GB | not measured on GB10 | No reason to prefer these over the shared Lightning instance. |

### 3.4 Embeddings (Mem0)

| Model | Dim | MTEB | Notes |
|---|---|---|---|
| **Qwen3-Embedding-4B (current)** | 2560 (MRL down to 32) | multilingual 69.45, English v2 74.60 ([card][hf-qemb]) | **Keep.** Truncating to a smaller dimension through MRL still means re-embedding (§6). |
| Qwen3-Embedding-8B (on disk) | 4096 | multilingual 70.58 | +1.1 points for twice the memory and a **full re-embed** to 4096-d. Not worth it. |
| Nemotron-3.5-Embed-1B | 2048 | MTEB not reported; multimodal retrieval ([card][hf-nemb]) | Different dimension, so a re-embed. No text-MTEB evidence of a gain. |
| EmbeddingGemma 2 (Sep 2026) | 768 (MRL 128–768) | multilingual 61.36, English 68.46 ([card][hf-egemma]) | Tiny (~0.3B text), but 8 points worse multilingual, plus a re-embed. |

### 3.5 KV cache arithmetic (from `config.json`)

| Model | Layers holding KV | KV per token (FP8) | 128K context, one sequence |
|---|---|---|---|
| Qwen3.6-35B-A3B | 10 of 40 (2 KV heads × 256) | ~10 KB | ~1.3 GB, plus small DeltaNet state |
| Nemotron 3.5 Lightning | a few attention layers out of 52 (2 KV heads × 128); the rest are Mamba-2 | a few KB | well under 1 GB, plus Mamba state |
| gpt-oss-120b | 18 full + 18 sliding-window(128) (8 KV heads × 64) | ~18 KB on full layers | ~2.4 GB |

The hybrids make long context cheap, so pool size is set by weights plus CUDA graphs and
activations, not by KV. NVIDIA's own LiLiCorr recipe for Super VL still takes most of the box at
0.72–0.80 because of the drafter and the 1M-capable state ([forum][f-supervl]).

---

## 4. Runtimes on GB10

| Runtime | Several models at once | Structured outputs | Speculative decoding on GB10 | Known GB10 gotchas |
|---|---|---|---|---|
| **vLLM** (0.28–0.32, nightly for new models) | One model per process. `--gpu-memory-utilization` is a fraction of **total** memory and is checked against **free** memory at start, so start the largest first and keep the sum ≤ ~0.6 with other tenants ([dev.to][devto-residency], [forum][f-twomodels]). | xgrammar / guidance, `response_format: json_schema`; with Qwen3-Coder reasoning set `enable_in_reasoning` ([docs][vllm-so]). Server-wide `--default-chat-template-kwargs '{"enable_thinking": false}'` ([docs][vllm-reason]). | MTP, DSpark, DFlash and LiLiCorr all work. Gains of 1.15–2× at short context; acceptance falls with depth (6.5 → 2.7 for LiLiCorr; 55% → 37–40% for DSpark at 32K). | NVFP4 without the right kernels falls back to emulation (~70× slower; check the kernel path in the log) ([forum][f-measured]). cuDNN FP8 GEMM crash: force cublas ([forum][f-twomodels]). Exits 0 on `EngineDeadError`, so use `Restart=always`. Run `vm.compaction_proactiveness=0` against kcompactd stalls. First request JIT takes ~25 s; weight load takes 10–15 min ([vLLM blog][vllm-spark]). Freezes when gmu > 0.8 ([forum][f-gemma-freeze]). |
| **SGLang** | One model per process | xgrammar JSON schema | DSpark / DFlash2: the fastest path for dense Qwen3.8-27B (36.6–47.9 tok/s) ([forum][f-q38-27b]) | Day-0 images per model (`lmsysorg/sglang:nemotron3-5-super`, …), less pinned than vLLM |
| **llama.cpp** (`llama-server`) | **Router mode** (`--models-preset`, `--models-max`, LRU, one process per model) ([glukhov][llama-router]) | GBNF and `json_schema` | DFlash for Bolt (+56%) | Build with `CMAKE_CUDA_ARCHITECTURES=121`. Drop the page cache before loading a 66 GB GGUF ([nabe2030][gh-nabe]). Slot save fails in router mode. |
| **Ollama** | Loads and unloads several models by `keep_alive`; good for small resident models | `format` takes a JSON schema; `think:false` per request ([docs][ollama-think]) | MTP variants of Qwen3.6 | **No batching**: 8× equals single-stream throughput ([forum][f-measured]); ~30% slower than vLLM on big MoEs |
| **TensorRT-LLM** | One engine per process | guided decoding | yes | Spark configs exist for Super VL (max batch 2, 131K) ([card][hf-supervl]); a heavier build-and-patch cycle than vLLM |

**Verdict.** Use vLLM for the two chat models: recipes for GB10 exist, and it batches. Keep
Ollama for the always-on small models (embedding, `qwen3:8b`). SGLang matters only for dense
models that need its drafters. llama.cpp router mode is the fallback for GGUF-only models
(Bolt, Q2 DeepSeek).

---

## 5. Local vs cloud economics

**Assumptions.** A decide call is about **3,000 input + 250 output tokens**; read the real
figure from `usage_daily`. 30,000 decide calls a month. A "heavy OpenCode day" is 20M input
tokens + 0.3M output, priced with **no cache discount** (an upper bound). OpenRouter list prices
were pulled 2026-11-11 from `/api/v1/models`.

| Model (OpenRouter id) | $/M in | $/M out | 1,000 decides | Decide per month | Heavy OpenCode month |
|---|---|---|---|---|---|
| `nvidia/nemotron-3.5-lightning` | 0.07 | 0.20 | $0.26 | $7.8 | $44 |
| `nvidia/nemotron-3-super-120b-a12b` | 0.075 | 0.40 | $0.33 | $9.8 | $49 |
| `nvidia/nemotron-3.5-super-vl` | 0.15 | 0.47 | $0.57 | $17 | $94 |
| `qwen/qwen3.6-35b-a3b` | 0.10 | 0.90 | $0.53 | $16 | $68 |
| `qwen/qwen3-coder-next` | 0.12 | 0.80 | $0.56 | $17 | $79 |
| `qwen/qwen3.8-flash` | 0.15 | 0.47 | $0.57 | $17 | $94 |
| `openai/gpt-oss-120b` | 0.15 | 0.60 | $0.60 | $18 | $95 |
| `deepseek/deepseek-v4.1-flash` | 0.051 | 0.567 | $0.29 | $8.8 | $36 |
| `z-ai/glm-5.3-flash` (321B, too big to host) | 0.04 | 0.078 | $0.14 | $4.2 | $25 |
| `x-ai/grok-4.6` (API list price, for scale) | 2.00 | 6.00 | $7.50 | $225 | $1,254 |
| SuperGrok OAuth (today) | – | – | $0 marginal | $0 | $0 marginal; draws on the shared weekly pool |

**What this means:**

- **Trading:** local saves **≤ ~$18 a month** against OpenRouter, and $0 against SuperGrok.
  Running decide locally is about **independence** (no xAI quarantine, no OpenRouter outage,
  no spend cap) and **latency control**, not money. So keep it small, fast and cheap in memory:
  Lightning.
- **OpenCode and Hermes:** heavy use costs **$25–95 a month** on cheap open-model APIs, and
  much more on Grok list prices. Under SuperGrok the cost is the shared weekly usage pool:
  third-party reports say paid plans moved to **one weekly pool across Chat, Imagine, Voice and
  Build** in June 2026, and the subscription costs about $30 a month ([buildfastwithai][sg-1],
  [tooljunction][sg-2]; no official xAI page was found). Hermes on grok-4.x and Grok Build
  compete for that pool. **These workloads are the ones worth running locally.**
- **Mem0:** negligible in volume; keep it local for privacy.
- **Power:** not measured here. The box runs around the clock either way, so the marginal cost
  is serving versus idling. GPU spikes under load are 72–80 W ([forum][f-supervl]).

---

## 6. Migration steps (Plan B), with risks

Each step lists its verification. Nothing here has been run; the operator applies it.

| # | Step | Verify | Risk |
|---|---|---|---|
| 1 | **Stop `nemotron-vllm` (Nemotron 3 Super).** Keep the weights on disk for rollback. | `free -g` shows ~85 GB more available | None. It has served zero requests. Its fallback role is covered by step 2. |
| 2 | Start **Lightning NVFP4** on `:8009` from NVIDIA's Spark recipe, with `--gpu-memory-utilization 0.22`, `--max-model-len 32768`, `--max-num-seqs 4` and the DSpark drafter. Set `TRADER_NEMOTRON_MODEL` to its served name. Pin the image by digest. | A Probe passes. A decide on a synthetic context returns schema-valid JSON at ≥60 tok/s. The thinking guard still applies because the URL is unchanged. | Lightning is weaker than 3 Super; see the replay gate in §1. |
| 3 | Apply the GB10 fixes: FlashInfer FP8 → cublas patch, `Restart=always`, `vm.compaction_proactiveness=0`, and a MemAvailable watchdog (kill below 12 GB) for first starts ([forum][f-twomodels]). Keep the clock-cap unit. | 72 h with no `EngineDeadError` | Patches have to be re-applied when the image is bumped |
| 4 | Start **Qwen3.6-35B-A3B NVFP4** on a new port with `--gpu-memory-utilization 0.30`, `--max-model-len 131072`, `--max-num-seqs 4`, the tool-call and reasoning parsers matched to the model, and MTP. Start it **after** Lightning; vLLM checks free memory. | A tool-call round trip from OpenCode and from Hermes. Owners found mismatched `--tool-call-parser` / `--reasoning-parser` flags behind most "the model can't call tools" failures ([forum][f-measured]). | Reported NVFP4 hang and Marlin fallback ([forum][f-q36hang], [ai-muninn][muninn-trap]); fall back to the FP8 checkpoint (~35 GB, then gmu 0.36). |
| 5 | Point OpenCode and one Hermes host at the Qwen3.6 endpoint (LAN only, no auth, same trust model as `:8009`). Keep Grok as Hermes's fallback provider. | Hermes usage on the `hermes-usage` Grafana dashboard drops for the moved host | Joint load halves each model's throughput. The trader's `TRADER_LOOP_MAX_CONCURRENT_DECIDES=2` is already the right cap. |
| 6 | *(Optional, frees 6.6 GB)* Move Mem0 extraction from `qwen3:8b` to Lightning with the OpenAI provider. Thinking off must come from the server default (`--default-chat-template-kwargs '{"enable_thinking": false}'`), because stock Mem0 sends no `chat_template_kwargs`. The preset still sends its own. | `mem0_add(infer=True)` returns facts, not an empty list | Mem0 then depends on the trading backstop instance; a Lightning restart pauses memory writes. The Hermes Mem0 plugin (`_backend.py`) needs the same change. |
| 7 | **Leave the embedder alone.** If a later change of embedding model or dimension is ever wanted: create new collections (`tomas`, `elizabeth`, `marcela`) with the same dense + `bm25` sparse layout, re-embed every memory from its stored text, switch every client (Claude Code `qdrant_key.py` config and the Hermes Mem0 plugin on every host) at once, then drop the old collection. **A dimension change cannot be done in place**: vectors of different sizes cannot share a collection, and mixed embedders return garbage similarity. | Search results stay the same on a fixed query set | Cost: every memory in three collections, plus a coordinated client cut-over across the Hermes fleet |

**Rollback:** stop both new instances and restart `nemotron-vllm` at 0.70. The decide preset is
unchanged apart from `TRADER_NEMOTRON_MODEL`.

---

## 7. What could not be verified

- **Runtime memory** of the NVFP4 checkpoints. The budgets use HF repo size plus the configured
  gmu fraction, not measured RSS. Measure with `nvidia-smi` after each start (Docker's own figure
  is misleading: 7.7 GiB reported against 52.9 GiB actual, [forum][f-twomodels]).
- **JSON-schema reliability of Lightning and Qwen3.6 on Salamander's decide schema.** No
  published test matches it; the replay gate is the test. The only measured tool-call scores
  found were Bolt's (§3.1) and Qwen3.8-Flash-Next's tool-eval-bench (89–93/100).
- **Decide prompt size.** The 3,000/250-token assumption sets the cost column; `usage_daily` has
  the real figure.
- **SuperGrok terms.** Price and the shared weekly pool come from third-party sites, which also
  disagree on whether API-style OAuth use draws on the pool. No official xAI source was found.
- **Some throughput figures are self-reported or come from search snippets.** In particular:
  Bolt's 140 tok/s (author) against 70.8 (an independent owner); Lightning's 124 tok/s SGLang
  claim; the Qwen3.6 108 tok/s MTP figure; Lightning's 78.5 → 90.7 tok/s with DSpark, which
  came from a search summary of the forum thread that I did not open. The tables use the
  ranges.
- **Nemotron 3.5 Super VL against 3 Super on the same benchmarks.** The card compares only
  BF16 with NVFP4.
- **Licence details** of OpenMDW-1.1, qwen-community-1.0 and minimax-community were not read in
  full. Everything recommended here is either Apache-2.0 or OpenMDW-1.1, which the card states
  is "ready for commercial or non-commercial use".

---

## Sources

[f-twomodels]: https://forums.developer.nvidia.com/t/two-models-on-one-dgx-spark-qwen3-6-35b-nvfp4-nemotron-3-5-lightning-with-dspark-numbers-under-shared-load-and-a-fix-for-cudnn-fp8-crashes/386489
[f-supervl]: https://forums.developer.nvidia.com/t/nemotron-3-5-super-vl-120b-a12b-nvfp4-on-a-single-dgx-spark-34-49-tok-s-decode-with-lilicorr-16k-thermal-shutdowns-at-32k/385991
[f-boltsuper]: https://forums.developer.nvidia.com/t/nemotron-3-5-bolt-and-super-vl-on-one-spark-with-one-command/386093
[f-measured]: https://forums.developer.nvidia.com/t/measured-inference-benchmarks-on-a-single-dgx-spark-same-harness-across-ollama-llama-cpp-and-vllm-notes-data-published/379766
[f-lightning-search]: https://forums.developer.nvidia.com/t/new-nemotron-3-5-lighting-30b-a3b/379832
[f-q38-27b]: https://forums.developer.nvidia.com/t/comprehensive-qwen3-8-27b-study-on-dgx-sparks-quantization-speculative-decoding-and-tp-dp-scaling/381102
[f-q38flash]: https://forums.developer.nvidia.com/t/qwen3-8-flash-next-on-1-2-and-4-dgx-sparks-with-nvidias-official-nvfp4-quant-64-tok-s-peak-single-stream/382476
[f-coding]: https://forums.developer.nvidia.com/t/single-spark-setups-which-models-do-you-actually-run-for-coding-and-how-sharing-mine-a-test-prompt/374423
[f-q36hang]: https://forums.developer.nvidia.com/t/qwen3-6-35b-a3b-nvfp4-hangs-after-attention-backend-selection-across-3-vllm-images-including-nvidias-own-official-recipe/373274
[f-gemma-freeze]: https://forums.developer.nvidia.com/t/gemma-4-on-dgx-spark-gb10-system-freeze-at-80-utilization-sm-121-kernel-issues/366060
[pi3g]: https://pi3g.com/dgx-spark-benchmarking-on-inference-performance-of-14-open-weight-models/
[cm-supervl]: https://dev.classmethod.jp/en/articles/dgx-spark-nemotron-3-5-super-vl-first-touch/
[cm-bolt]: https://dev.classmethod.jp/en/articles/dgx-spark-nemotron-3-5-bolt-74b-a9b-first-touch/
[cm-qwen36]: https://dev.classmethod.jp/en/articles/dgx-spark-qwen3-6-35b-a3b-nvfp4-new-champion/
[muninn-trap]: https://ai-muninn.com/en/blog/dgx-spark-nvfp4-trap-gb10-fp8-wins
[muninn-q38]: https://ai-muninn.com/en/blog/qwen38-flash-next-nvfp4-dgx-spark-vllm-recipe
[gh-q38]: https://github.com/tonyd2wild/Qwen3.8-Flash-Next-NVFP4-DGX-Spark
[gh-nabe]: https://github.com/nabe2030/nemotron-3-super-dgx-spark/blob/main/README.md
[jh]: https://jetsonhacks.com/wp-content/uploads/2025/10/spark-llamacpp-bench.html
[vllm-spark]: https://vllm.ai/blog/2026-06-01-vllm-dgx-spark
[vllm-so]: https://docs.vllm.ai/en/latest/features/structured_outputs.html
[vllm-reason]: https://docs.vllm.ai/en/latest/features/reasoning_outputs.html
[ollama-think]: https://docs.ollama.com/capabilities/thinking
[llama-router]: https://glukhov.org/llm-hosting/llama-cpp/llama-server-router-mode/
[devto-residency]: https://dev.to/ric03uec/two-qwen3-models-on-one-dgx-spark-the-residency-math-for-local-llm-coding-5bpj
[hf-supervl]: https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4
[hf-lightning]: https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4
[hf-bolt]: https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB
[hf-q38flash]: https://huggingface.co/Qwen/Qwen3.8-Flash-Next
[hf-minimax]: https://huggingface.co/MiniMaxAI/MiniMax-M3.1-Flash
[hf-qemb]: https://huggingface.co/Qwen/Qwen3-Embedding-4B
[hf-nemb]: https://huggingface.co/nvidia/Nemotron-3.5-Embed-1B-BF16
[hf-egemma]: https://huggingface.co/google/embeddinggemma-2
[nv-cli]: https://build.nvidia.com/spark/cli-coding-agent/overview
[aliteq]: https://aliteq.com/web-stories/best-local-coding-model-for-opencode
[hermes-guide]: https://www.developersdigest.tech/blog/hermes-agent-guide-2026
[sg-1]: https://www.buildfastwithai.com/blogs/supergrok-explained-pricing-limits-is-it-worth-it-2026
[sg-2]: https://www.tooljunction.io/blog/grok-pricing-usage-limits-2026

- Forum, two models on one Spark (Qwen3.6-35B + Lightning): <https://forums.developer.nvidia.com/t/two-models-on-one-dgx-spark-qwen3-6-35b-nvfp4-nemotron-3-5-lightning-with-dspark-numbers-under-shared-load-and-a-fix-for-cudnn-fp8-crashes/386489>
- Forum, Nemotron 3.5 Super VL on one Spark, thermal shutdowns and clock cap: <https://forums.developer.nvidia.com/t/nemotron-3-5-super-vl-120b-a12b-nvfp4-on-a-single-dgx-spark-34-49-tok-s-decode-with-lilicorr-16k-thermal-shutdowns-at-32k/385991>
- Forum, Bolt and Super VL on one Spark: <https://forums.developer.nvidia.com/t/nemotron-3-5-bolt-and-super-vl-on-one-spark-with-one-command/386093>
- Forum, same harness across Ollama, llama.cpp and vLLM: <https://forums.developer.nvidia.com/t/measured-inference-benchmarks-on-a-single-dgx-spark-same-harness-across-ollama-llama-cpp-and-vllm-notes-data-published/379766>
- Forum, Qwen3.8-27B study: <https://forums.developer.nvidia.com/t/comprehensive-qwen3-8-27b-study-on-dgx-sparks-quantization-speculative-decoding-and-tp-dp-scaling/381102>
- Forum, Qwen3.8-Flash-Next on 1, 2 and 4 Sparks: <https://forums.developer.nvidia.com/t/qwen3-8-flash-next-on-1-2-and-4-dgx-sparks-with-nvidias-official-nvfp4-quant-64-tok-s-peak-single-stream/382476>
- Forum, single-Spark coding setups: <https://forums.developer.nvidia.com/t/single-spark-setups-which-models-do-you-actually-run-for-coding-and-how-sharing-mine-a-test-prompt/374423>
- Forum, Nemotron 3.5 Lightning: <https://forums.developer.nvidia.com/t/new-nemotron-3-5-lighting-30b-a3b/379832>
- pi3g, 14 models on vLLM 0.29: <https://pi3g.com/dgx-spark-benchmarking-on-inference-performance-of-14-open-weight-models/>
- vLLM on DGX Spark (blog): <https://vllm.ai/blog/2026-06-01-vllm-dgx-spark>
- OpenRouter models API: <https://openrouter.ai/api/v1/models>
- Hugging Face model cards and API: the `hf-*` links above, plus `https://huggingface.co/api/models/<id>` for sizes and licences
