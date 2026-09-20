# Low-Latency Voice Assistant (Rime Hackathon — Perceived Response Time)

A voice assistant that measures and minimizes the delay between "user
stops talking" and "user hears the first sound back" — end to end, not
just the TTS leg.

## The user and the problem

**User:** Kira is a hands-free voice assistant for field workers and
drivers -- delivery drivers, technicians, warehouse staff -- anyone whose
hands and eyes are occupied and who cannot glance at a screen to check
whether the assistant is "thinking."

**Why voice specifically, and why latency specifically:** for this user,
a laggy assistant isn't just annoying, it's actively worse than no
assistant at all -- a driver who has to wait 2+ seconds of dead air after
asking a question is more distracted than if they'd just pulled over to
check their phone. A chatbot with a play button doesn't need to care
about time-to-first-audio, because the user is already looking at a
screen. Kira's user is not. If you removed voice from this product and
replaced it with a text interface, the product would be actively unsafe
for the target use case, not just less convenient -- that's the bar this
project is built against.

This project treats "perceived response time" as a first-class, measured
metric rather than an assumption.

## Architecture

```
User mic  ─▶ LiveKit room ─▶ Silero VAD ─▶ Groq STT (Whisper) ─▶ Groq LLM (gpt-oss-120b)
                                                              │
                                                              ▼
User speaker ◀─ LiveKit room ◀── audio playback ◀── Rime TTS (WebSocket streaming)
```

- **Transport / turn-taking / orchestration:** LiveKit Agents
- **VAD:** Silero
- **STT:** Groq Whisper (`whisper-large-v3-turbo`)
- **LLM:** Groq (`openai/gpt-oss-120b` — Llama 3.3 70B was deprecated by Groq on 2026-06-17 and is no longer served)
- **TTS (the piece this hackathon is judged on):** Rime
  - **Model:** `coda` (Rime's flagship quality model)
  - **Speaker:** `celeste`
  - **Language:** `eng`
  - **Transport:** WebSocket streaming (`use_websocket=True` on the LiveKit plugin), not the default HTTP mode — this is the variable under test
  - **Endpoint:** `wss://users-ws.rime.ai/ws3` (Rime's flagship JSON WebSocket protocol — confirmed in `benchmark.py` against Rime's WebSocket API reference; connection params are passed as URL query params, not JSON body)
  - **Audio format:** not explicitly overridden in `agent.py` — uses the LiveKit Rime plugin's default. `benchmark.py`'s standalone HTTP/WebSocket calls explicitly request `mp3`.

## What's in this repo

| File | Purpose |
|---|---|
| `agent.py` | Full voice pipeline; logs per-turn latency to `latency_log.csv` |
| `benchmark.py` | Standalone script that isolates just the Rime TTS leg (HTTP vs WebSocket TTFB), no LiveKit/OpenAI required |
| `RIME_EVIDENCE.md` | The acceptance test: claim, procedure, how to reproduce, limitations |
| `.env.example` | Placeholders only — copy to `.env` and fill in real keys |
| `requirements.txt` | Pinned dependencies |

## Setup

1. **Get API keys**
   - Rime: sign up at [app.rime.ai/signup](https://app.rime.ai/signup/), copy your key from [API Tokens](https://app.rime.ai/tokens/)
   - Groq: [console.groq.com/keys](https://console.groq.com/keys) — free tier, no billing method required
   - LiveKit Cloud: [cloud.livekit.io](https://cloud.livekit.io) — copy WS URL, API key, API secret

2. **Install**
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # then fill in your real keys
   ```

3. **Run the isolated TTS benchmark first** (fastest way to confirm your
   Rime key works and to see the streaming-vs-HTTP latency gap on its own):
   ```bash
   export RIME_API_KEY=your_key   # or rely on .env if you load it yourself
   python benchmark.py
   ```
   Expect a printed table plus `benchmark_results.csv`.

4. **Run the full voice agent**
   ```bash
   python agent.py dev
   ```
   Then either open LiveKit's [Agents Playground](https://agents-playground.livekit.io/)
   or, more simply, use the **Console** built into your LiveKit Cloud
   dashboard (Agents → select your worker → Start session) — both connect
   to the same running worker. Talk to it, and watch `latency_log.csv`
   fill in with one row per turn.

## Failure behavior (observed, not hypothetical)

Three real failures occurred during development and are disclosed here
rather than smoothed over:

1. **Groq model deprecation** — `llama-3.3-70b-versatile` returned a 404
   `model_not_found` error partway through development because Groq
   deprecated it on 2026-06-17. The agent's LLM call failed outright with
   no fallback, which meant no text ever reached Rime, so there was no
   audio output and no error visible in the Console UI — only in the raw
   worker log. Fixed by switching to `openai/gpt-oss-120b`. **Take-away
   for judges:** if a model name in this repo ever 404s again, check
   Groq's current model list before assuming the code is broken.
2. **Rime WebSocket transient disconnect** — observed in production use:
   `Rime ws closed unexpectedly, status_code=-1, retryable=True`. The
   LiveKit Rime plugin retried automatically within ~0.1s and the turn
   completed successfully, but with a measurable latency cost (that
   turn's `tts_ttfb` was ~1.3s vs. a typical ~0.3-0.5s). This is disclosed
   as a limitation below rather than hidden.
3. **A LiveKit Agents API mismatch** — `metrics.MetricsCollectedEvent`
   does not exist in `livekit-agents` 1.8.0 (the event class lives at
   `livekit.agents.MetricsCollectedEvent` directly). Registering the
   callback with the wrong type annotation crashed `inspect.signature()`
   internally, silently killing the whole session with no audio and no
   obvious error in the Console. Fixed by correcting the import.

## Known limitations (disclose per hackathon rules)

- `latency_log.csv` measures **server-side** perceived response time (VAD end-of-utterance
  → first audio byte from Rime). It does **not** include client-side network
  RTT or the receiving device's audio buffer fill time — those vary by
  device/network and should be measured separately with a client-side
  timestamp if you need a true "sound leaves the user's speaker" number.
- `benchmark.py`'s WebSocket message schema is a best-effort implementation
  based on the documented default endpoint (`wss://users-ws.rime.ai`); the
  exact frame protocol should be verified against Rime's WebSocket API
  reference before you cite these numbers as final evidence.
- The `metrics.EOUMetrics` / `LLMMetrics` / `TTSMetrics` field names in
  `agent.py` reflect the `livekit-agents` version pinned in
  `requirements.txt`. If you upgrade that dependency, re-check the field
  names before trusting the logged numbers.
- No fallback TTS provider is wired in. If Rime is unreachable, the agent
  will error rather than silently degrading — acceptable for a hackathon
  demo, but call this out if judges ask about production-readiness.

## Cost

- **Rime:** paid, covered by your account credit — this is the one piece the hackathon actually judges, so it's the one worth spending real credit on.
- **Groq:** free tier, no billing method required, used here for STT (Whisper) and LLM (`openai/gpt-oss-120b`) specifically to keep this project runnable at $0 outside of Rime.
- **LiveKit Cloud:** free tier is sufficient for development and this demo.

## Third-party services

Rime (TTS), Groq (STT + LLM), LiveKit Cloud (realtime transport). All
credentials are read from environment variables / `.env`, never hardcoded.
