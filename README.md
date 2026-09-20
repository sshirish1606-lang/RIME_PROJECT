# Kira: Low-Latency Voice Assistant

A hands-free voice assistant that measures and minimizes the delay between the end of a user's speech and the first audio played back. Built for the Rime Hackathon (Perceived Response Time track).
Latency is tracked end to end, not just for the TTS step.

## Overview
Target users: field workers and drivers, such as delivery drivers, technicians, and warehouse staff. Their hands and eyes are busy, and they can't check a screen to see whether the assistant is still working.
Why latency matters:** for these users, dead air is a safety issue. Waiting more than two seconds for an answer is more distracting than pulling over to check a phone. A text chatbot with a play button can ignore time-to-first-audio because the user is already looking at the screen. Kira can't. Replacing voice with a text interface would make the product unsafe for its intended use, so response time is treated as a primary, measured metric.

## Architecture
User mic ─▶ LiveKit room ─▶ Silero VAD ─▶ Groq STT (Whisper) ─▶ Groq LLM (gpt-oss-120b)
│
▼
User speaker ◀─ LiveKit room ◀── audio playback ◀── Rime TTS (WebSocket streaming)

| Component | Choice |
|---|---|
| Transport, turn-taking, orchestration | LiveKit Agents |
| VAD | Silero |
| STT | Groq Whisper (`whisper-large-v3-turbo`) |
| LLM | Groq `openai/gpt-oss-120b` (Llama 3.3 70B was deprecated by Groq on 2026-06-17) |
| TTS | Rime |

### Rime TTS configuration

Rime TTS is the component under evaluation.

- **Model:** `coda`
- **Speaker:** `celeste`
- **Language:** `eng`
- **Transport:** WebSocket streaming (`use_websocket=True` in the LiveKit plugin) instead of the default HTTP mode. This is the variable being tested.
- **Endpoint:** `wss://users-ws.rime.ai/ws3` (JSON WebSocket protocol; connection parameters are passed as URL query params, not in a JSON body)
- **Audio format:** `agent.py` uses the LiveKit Rime plugin's default. The standalone calls in `benchmark.py` explicitly request `mp3`.

## Repository Contents

| File | Purpose |
|---|---|
| `agent.py` | Full voice pipeline. Logs per-turn latency to `latency_log.csv`. |
| `benchmark.py` | Standalone script that isolates the Rime TTS step and compares HTTP vs. WebSocket TTFB. Needs only a Rime key. |
| `RIME_EVIDENCE.md` | Acceptance test: claim, procedure, reproduction steps, limitations. |
| `.env.example` | Placeholder environment variables. Copy to `.env` and fill in real keys. |
| `requirements.txt` | Pinned dependencies. |

## Setup

### 1. Get API keys

- **Rime:** sign up at [app.rime.ai/signup](https://app.rime.ai/signup) and copy your key from **API Tokens**.
- **Groq:** create a key at [console.groq.com/keys](https://console.groq.com/keys). The free tier requires no billing method.
- **LiveKit Cloud:** sign up at [cloud.livekit.io](https://cloud.livekit.io) and copy the WebSocket URL, API key, and API secret.

### 2. Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and add your keys.

### 3. Run the TTS benchmark

This confirms your Rime key works and shows the HTTP vs. WebSocket latency gap on its own.

```bash
export RIME_API_KEY=your_key
python benchmark.py
```

The script prints a results table and writes `benchmark_results.csv`.

### 4. Run the voice agent

```bash
python agent.py dev
```

Connect using either the LiveKit Agents Playground or the Console in your LiveKit Cloud dashboard (**Agents → select your worker → Start session**). Both connect to the same running worker. Each conversation turn adds one row to `latency_log.csv`.

## Issues Encountered During Development

**Groq model deprecation.** `llama-3.3-70b-versatile` began returning a 404 `model_not_found` error after Groq deprecated it on 2026-06-17. With no fallback, no text reached Rime, so no audio was produced. The Console UI showed no error; it only appeared in the raw worker log. The fix was switching to `openai/gpt-oss-120b`. If a model name in this repo returns a 404 in the future, check Groq's current model list first.

**Rime WebSocket transient disconnect.** One turn logged `Rime ws closed unexpectedly, status_code=-1, retryable=True`. The LiveKit Rime plugin reconnected within about 0.1s and the turn completed, but `tts_ttfb` for that turn was about 1.3s, against a typical 0.3–0.5s.

**LiveKit Agents API mismatch.** `metrics.MetricsCollectedEvent` does not exist in `livekit-agents` 1.8.0; the class is exposed as `livekit.agents.MetricsCollectedEvent`. Using the wrong type annotation on the callback made `inspect.signature()` fail internally, which silently ended the session with no audio and no visible error in the Console. Correcting the import fixed it.

## Limitations

- **Server-side measurement only.** `latency_log.csv` records the time from VAD end-of-utterance to the first audio byte from Rime. It excludes client-side network RTT and audio buffer fill time. Measure those separately with a client-side timestamp to get the true "sound leaves the speaker" latency.
- **Benchmark protocol.** The WebSocket message schema in `benchmark.py` is a best-effort implementation. Verify the frame protocol against Rime's WebSocket API reference before citing the numbers as final.
- **Dependency versions.** The `EOUMetrics`, `LLMMetrics`, and `TTSMetrics` field names in `agent.py` match the `livekit-agents` version pinned in `requirements.txt`. Re-check them after any upgrade.
- **No TTS fallback.** If Rime is unreachable, the agent errors out instead of switching providers. This is acceptable for a demo but not for production.

## Cost

| Service | Cost |
|---|---|
| Rime | Paid, covered by account credit. This is the component under evaluation. |
| Groq | Free tier, used for STT and the LLM so the project runs at no cost outside of Rime. |
| LiveKit Cloud | Free tier is sufficient for development and the demo. |

## Third-Party Services

Rime (TTS), Groq (STT and LLM), and LiveKit Cloud (realtime transport). All credentials are read from environment variables or `.env` and are never hardcoded.
