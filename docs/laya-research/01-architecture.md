# Laya: architecture, tokenizer and weight format

Scope: what the local classifier artifact is, so others can judge conversion to another runtime.
Evidence gathered from the local HF cache, the laya-mlx venv and the Hugging Face API.
Gate source revision: hermes-fleet-manager `1c1cca0dcfb3d01772b9d9f614ef679678a3ba90` (laya_gate.py, 1261 lines).

## 1. Summary

- Laya is NOT a generative LM. It is a ModernBERT-large bidirectional encoder plus a small
  decision head (2 transformer layers), a scoring head and an action head.
- It emits 0 tokens. One forward pass gives one logit per option marker.
- Weights: 206 tensors, all F16, 421,293,830 parameters, 842,609,225 bytes. No quantization.
- Tokenizer: byte-level BPE (HF `tokenizer.json`, 50,280 merges-vocab entries, model vocab_size 50,368).
- Upstream FP32 checkpoint exists on Hugging Face (`convaiinnovations/laya`), PyTorch/safetensors
  with the reference Python code. The MLX weights are NOT the only source.
- The score the gate compares to 0.59 is a temperature-scaled 2-way softmax probability.

## 2. How the gate loads and calls the model

`laya_gate.py`:

- Line 29: `MODEL = os.environ.get("LAYA_GATE_MODEL", "aac6fef/laya-mlx")`
- Lines 623-649, `load_model()`: `import laya_mlx as laya`, then `_model = laya.load(MODEL)`.
  Loaded once under a lock, preloaded at server start (`_preload_model`).
- Lines 1171-1186: if `laya_mlx` is not importable the gate re-execs into
  `~/.hermes/venvs/laya-mlx/bin/python3` (override `LAYA_GATE_PYTHON`).
- Gate is an HTTP service on 127.0.0.1:11436 (`LAYA_GATE_PORT`).
- Inference, lane 3 (`classify`, lines ~850-870):

```python
state = (f"{DOCTRINE}\n\n"
         f"Situation: {context or 'no further detail available'}\n"
         f"Event: {probe}\n"
         f"Events of this kind on this session in the last 24 hours: {len(hist)}.")
out = load_model().predict(state, QUESTION)
ans = out.get("answers", out).get("action", {})
probs = ans.get("probabilities") or {}
conf = float(probs.get("interrupt her now") or ans.get("confidence") or 0.0)
escalate = bool(conf >= ESCALATE_IF_AT_LEAST)
```

- `ESCALATE_IF_AT_LEAST = float(os.environ.get("LAYA_GATE_CHOICE_THRESHOLD", "0.59"))` (line 104).
  Calibration note in the source (lines 96-102): over 337 decisions confidence runs 0.3094 to 0.8680,
  p75 = 0.5894.
- Other threshold constants: `RESPOND_IF_AT_LEAST` 0.55 (line 143); `THRESHOLD` 0.80 (line 30)
  is defined but the lane-3 path in `classify` uses `ESCALATE_IF_AT_LEAST`, not `THRESHOLD`.
- Same `predict` path is also used by `_ask()` (line 983) for the respond question (6 options)
  and `affirm()` (risk = 1 - P("safe to affirm")).

## 3. Score semantics

Score = `answers.action.probabilities["interrupt her now"]`. Type: softmax probability over
the 2 options, after temperature scaling. Not a raw logit, not a sigmoid, not a token logprob.

From `laya_mlx/agent.py` `system_one`:

```python
k, qt = len(item["markers"]), item["qtype"]
scale = self.temperature_by_options.get(temp_bucket(qt, k), self.temperature[qt])
z = logits[row, :k] / scale
p = np.exp(z - z.max()); p /= p.sum()
```

- `temp_bucket(choice, 2)` = `"choice:2"`, shipped temperature 1.9063563346862793 (`rl_agent_config.json`).
  Inside the clamp [0.5, 5.0], so applied unchanged.
- Rounded to 4 decimals. Logits come from `scorer` (LayerNorm, Linear, GELU, Linear(dims,1))
  applied at the marker positions, masked to `-1e4` for unused slots.
- Fallback `ans.get("confidence")` is normalized-entropy confidence `1 - H(p)/log(k)`, a different
  quantity. It is only reached when the probability is 0.0 or missing (an `or` on a float also
  swallows a genuine 0.0).
- Because a 2-way softmax divided by 1.906, scores are compressed toward 0.5. With two options,
  P(interrupt) >= 0.59 means (gap/1.906) >= ln(0.59/0.41) = 0.364, so the raw logit gap must be >= 0.694.
- A separate `act_head` output (`act_probability`) exists in the result and is NOT used by the gate.

## 4. Exact inputs to the model

Two arguments to `predict(state, questions)`:

1. `state` (string), built as shown in section 2. `DOCTRINE` (laya_gate.py lines 77-83) is:

```
The operator runs a fleet of coding agents. Every event raised to her interrupts her, and she has asked to hear only about a decision that only she can make, a genuine stop, or anything touching production, money, security or a client. A session that is merely quiet is not a stop. A session that has finished its work is not a stop. A repeat of something already reported is not news.
```

   Fields: `context` (situation text), `probe` (= event `text` or `kind`), `len(hist)` (count of same
   family events in last 24h). Request body of `/decide`: `session`, `kind`, `text`, `context`.
2. `questions` = `QUESTION` (lines 84-90):

```python
{"action": {"type": "choice",
            "instructions": "What is the right action for the operator here?",
            "criteria": ["interrupt her now", "log it and stay silent"]}}
```

Token layout (`laya_mlx/common.py` `build_sequence`):

```
[CLS] choice question: <instructions> [SEP] [MASK] interrupt her now [MASK] log it and stay silent [SEP] <state tokens> [SEP]
```

- Max length 512 tokens total (`rl_agent_config.json` `max_len`), head (question plus options) capped at 192
  (`head_max_len`), each option at most 48 tokens.
- State is truncated from the RIGHT (`truncate_left=False` default; `agent.prepare` does not pass it).
  The `Event:` and 24h-count lines come last in `state`, so they are the first to be cut when the
  doctrine plus situation is long. Observation, not tested here: the respond/affirm path builds `situation()` with `SITUATION_CHARS` = 4000 chars
  (default in laya_gate.py), which is well over 512 tokens. UNKNOWN: how often truncation hits the Event line.
- Option marker positions (the `[MASK]` tokens) are where logits are read.
- The encoder sees the `qtype` embedding (`type_emb`, 3 types: choice, score, noul) added after the encoder.

## 5. The artifact: aac6fef/laya-mlx

Local cache: `/Users/terra/.cache/huggingface/hub/models--aac6fef--laya-mlx/`
Revision (`refs/main`): `20aed815fc6acde75733882e7ec0e3f28aeb9717` (matches the HF API `sha` today).
Snapshot dir: `.../snapshots/20aed815fc6acde75733882e7ec0e3f28aeb9717/`

Files (snapshot):

| file | bytes | note |
|---|---|---|
| model.safetensors | 842,609,225 | sha256 b9c07bf14be2fa5c78a9193a3e6d840ac80e89e62fc40f425834c3d8a6eaa3de (verified with shasum) |
| encoder/config.json | 2,083 | ModernBertForMaskedLM config |
| rl_agent_config.json | 746 | head layers, max_len, temperatures, training info |
| mlx_config.json | 290 | format laya-mlx v1, dtype float16, source revision |
| tokenizer/tokenizer.json | 3,583,228 | HF fast tokenizer |
| tokenizer/tokenizer_config.json | 308 | PreTrainedTokenizerFast |
| manifest.json | 1,746 | per-file sha256 |
| validation.json | 18,257 | FP32 and FP16 parity vs PyTorch |
| README.md, LICENSE, NOTICE | see section 8 | |

Note: there is NO top-level `config.json`. The model config is `encoder/config.json` plus `rl_agent_config.json`.

Encoder (`encoder/config.json`):

- `architectures: ["ModernBertForMaskedLM"]`, `model_type: modernbert`, `transformers_version 5.0.0`
- hidden_size 1024, intermediate_size 2624, 28 layers, 16 heads (head_dim 64), vocab_size 50368
- layer_types: full_attention every 3rd layer (0,3,6,...), sliding_attention otherwise, `local_attention` 128
- RoPE: full theta 160000, sliding theta 10000 (default, unscaled); max_position_embeddings 8192
- GeGLU MLP (`Wi` 1024 to 5248, `Wo` 2624 to 1024), no biases, LayerNorm without bias, `tie_word_embeddings` true
- Trained base named in `rl_agent_config.json`: `"encoder": "answerdotai/ModernBERT-large"`

Decision model on top (`laya_mlx/model.py` `DecisionModel`, tensor names in safetensors):

- `type_emb` Embedding(3, 1024)
- `head`: 2 PyTorch-style TransformerEncoderLayers (pre-norm, self_attn with `in_proj`, 16 heads... computed as `dims // 64`, FFN 1024 to 4096 to 1024, ReLU)
- `scorer`: LayerNorm, Linear(1024,1024), GELU, Linear(1024,1)
- `act_head`: Linear(1028,256), GELU, Linear(256, len(act_costs)+1 = 2)
- `temperature` buffer (3 values) is in the file but ignored; calibration uses `rl_agent_config.json`

Parameter count by group (from the safetensors header, commands in section 7):

```
206 tensors {'F16': 206}  total params 421293830
encoder 394,781,696 | head 25,192,448 | act_head 263,938 | scorer 1,052,673 | type_emb 3,072 | temperature 3
```

Training info (`rl_agent_config.json`): 7313 updates, 1 epoch, 1.96 h, bf16 amp, `fine_tuned_from_checkpoint: true`.
Calibration temperatures: `[1.637, 1.251, 1.983]` by qtype; by bucket `choice:2` 1.906, `choice:3-5` 1.760,
`choice:6-10` 1.000, `choice:11+` 0.1006 (laya_mlx clamps this to 0.5 and warns), `score:3-5` 1.251, `noul:2` 1.983.

## 6. Tokenizer

- `tokenizer/tokenizer.json` exists (3,583,228 bytes, sha256 6c8aaa9a...d30 per manifest).
- `model.type` = `BPE`; `normalizer` = NFC; `pre_tokenizer` = ByteLevel (add_prefix_space false, use_regex true).
  HF vocab dict has 50,280 entries; model embedding rows 50,368 (padded).
- `tokenizer_config.json`: `PreTrainedTokenizerFast`, cls `[CLS]`, sep `[SEP]`, pad `[PAD]`, mask `[MASK]`, unk `[UNK]`.
  IDs in encoder config: bos/cls 50281, eos/sep 50282, pad 50283.
- Loaded with the Rust `tokenizers` library, no Transformers (`laya_mlx/tokenizer.py`);
  padding and truncation disabled, sequence assembled by hand.
- `[MASK]` in input text is replaced by a space before tokenizing (injection guard in `common.py`).
- Standard ModernBERT tokenizer family (OLMo-derived BPE). UNKNOWN: whether the vocab is byte-identical to
  `answerdotai/ModernBERT-large`; not diffed.

## 7. Upstream and the laya_mlx package

- laya_mlx: version 0.2.0, `/Users/terra/.hermes/venvs/laya-mlx/lib/python3.12/site-packages/laya_mlx/`
  (dist-info `laya_mlx-0.2.0.dist-info`). Requires mlx >=0.32.2,<0.33 (darwin arm64 only), numpy,
  tokenizers, huggingface-hub. Python 3.12 venv, mlx 0.32.3 installed.
  Repo: github.com/mizorewww/laya-mlx. Upstream: github.com/NandhaKishorM/laya (commit
  6a5819129eb220570792e417e49723d697efd76f per NOTICE).
- It is an independent third-party port (reimplements ModernBERT in MLX, no torch). Weights are
  a name-remapped FP16 cast of upstream. `mlx_config.json`: `source: convaiinnovations/laya`,
  `revision: c5d78730f3493e4fe16d61507ef4b78eef7318cf`, `subfolder: null` (the ROOT checkpoint of upstream,
  not `multilingual/` or `typed-decisions/`).
- Name remap (`sanitize_weights`): `in_proj_weight` to `in_proj.weight`, `scorer.N`/`act_head.N` to `.layers.N`.
  Linear layouts unchanged from PyTorch. Verified tensors: 206 (manifest).
- Upstream `convaiinnovations/laya` (HF API, sha `7b928d828b7b0e022f929d9bd2e44165aa270148` at query time; the
  pinned source revision above is `c5d78730...`). Tags: transformers, safetensors, license:apache-2.0.
  Files include root `model.safetensors`, `config.json`, `encoder/config.json`, `rl_agent_config.json`,
  `tokenizer/*`, `rl_agent_api.py`, `rl_common.py`, `email_utils.py`, plus `multilingual/` and
  `typed-decisions/` variants. The root upstream is FP32 per `encoder/config.json` `dtype: float32`
  (README: "source weights themselves are FP16", conflicting; UNKNOWN which is right, upstream file size not fetched).
- Parity evidence (`validation.json`, README): FP16 matches upstream PyTorch MPS FP32 argmax on 63/63
  decisions, max calibrated probability difference 0.0054443; FP32 path max error 5.17e-06.

## 8. Model card and licence (paths only)

- Model card: `.../snapshots/20aed815fc6acde75733882e7ec0e3f28aeb9717/README.md` (front matter `license: apache-2.0`,
  `library_name: mlx`, `pipeline_tag: text-classification`, `base_model: convaiinnovations/laya`)
- Licence: `.../snapshots/20aed815fc6acde75733882e7ec0e3f28aeb9717/LICENSE` (10,173 bytes, Apache License 2.0 header)
- Notice: `.../snapshots/20aed815fc6acde75733882e7ec0e3f28aeb9717/NOTICE`
- Package licence copies: `laya_mlx-0.2.0.dist-info/licenses/{LICENSE,NOTICE}`
- Upstream HF: https://huggingface.co/convaiinnovations/laya (tag `license:apache-2.0`)

## 9. Commands run

```
cd ~/Developer/hermes-fleet-manager && git rev-parse HEAD
grep -n -i "laya_mlx\|threshold\|0.59\|score\|prompt\|max_\|logit\|softmax" laya_gate.py
sed -n 610,662p laya_gate.py; sed -n 765,900p laya_gate.py; sed -n 1084,1115p laya_gate.py
ls ~/.hermes/venvs/laya-mlx/lib/*/site-packages ; ls ~/.cache/huggingface/hub
find ~/.cache/huggingface/hub/models--aac6fef--laya-mlx -ls ; cat refs/main
cat snapshots/*/{mlx_config,encoder/config,manifest,rl_agent_config,tokenizer/tokenizer_config}.json README.md NOTICE
shasum -a 256 snapshots/*/model.safetensors snapshots/*/LICENSE
cat site-packages/laya_mlx/{__init__,tokenizer,convert,model,agent,common}.py
python3 (parse safetensors header: 8-byte LE length + JSON) -> tensors, dtypes, parameter sums
python3 (json.load tokenizer.json -> model.type, vocab len, normalizer, pre_tokenizer)
curl -s https://huggingface.co/api/models/convaiinnovations/laya   # sha, tags, siblings
curl -s https://huggingface.co/api/models/aac6fef/laya-mlx          # sha
```

## 10. Implications for conversion

- Architecture is plain ModernBERT-large (28 layers) plus a custom head. A stock ModernBERT exporter
  (ONNX, CoreML, GGUF) would cover the encoder only. GGUF/llama.cpp: ModernBERT support exists for embeddings,
  but the head, type embedding, marker pooling and temperature logic would need custom code. Unverified.
- Best conversion source is upstream `convaiinnovations/laya` (PyTorch, reference `rl_agent_api.py`), not the MLX file.
  The MLX file is a lossless FP16 cast with renamed tensors, so mapping back is a rename only
  (`in_proj.weight` back to `in_proj_weight`, drop `.layers.` in scorer/act_head).
- No quantization exists to undo. At 842 MB FP16, 421M params; INT8 or 4-bit would be a new conversion and
  needs re-validation against the 0.59 threshold, which was calibrated on FP16 outputs.
- The score depends on 3 details any port must copy: the marker-position pooling, the `choice:2`
  temperature 1.906, and right-side truncation at 512 tokens. Get any wrong and 0.59 means something else.
- Parity test exists: `validation.json` holds input hashes and FP32 expectations; the gate's own decision log
  holds 337+ recorded confidences usable as a regression set. UNKNOWN: whether the log stores the full input.
- Runtime needs only: BPE tokenizer (tokenizer.json, any HF-compatible tokenizer lib), ModernBERT with
  alternating local (window 128, inclusive distance <= 64) and global attention, two RoPE bases, and a 2-layer
  transformer head. Nothing generative, no KV cache.

## 11. Unknowns

- Upstream root checkpoint dtype and file size (config says float32, README says FP16 source).
- Whether the upstream revision `c5d78730...` is still the latest (HF API now shows `7b928d82...`; not diffed).
- Tokenizer vocab identity vs ModernBERT-large: not diffed.
- Frequency of Event-line truncation in lane-3 inputs.
- Whether the decision log retains full input text for building a regression set.
