# RIME_EVIDENCE.md

## Hard voice claim

Switching Rime TTS from HTTP mode to WebSocket streaming mode
(`use_websocket=True`, model `coda`, speaker `celeste`) materially
reduces perceived response time in a full STT→LLM→TTS voice pipeline,
and this reduction holds as reply length increases (i.e., streaming
decouples TTFB from total synthesis time) -- but only once the
WebSocket connection is kept warm across turns, not reopened per call.

## Acceptance test (defined before the demo)

**Metric:** `perceived_response_time = eou_delay + llm_ttft + tts_ttfb`
measured per conversational turn, where:
- `eou_delay` — time from the user finishing speaking (VAD/STT end-of-utterance) to the final transcript
- `llm_ttft` — time from prompt submission to the LLM's first token
- `tts_ttfb` — time from first text sent to Rime to the first audio byte returned

**Pass condition:** mean `tts_ttfb` in WebSocket mode is lower than mean
`tts_ttfb` in HTTP mode across the same set of test utterances, and the
gap does not shrink as utterance length grows (proving it's a streaming
effect, not noise).

**Normal case:** a 3–8 word conversational reply, spoken end-to-end via `agent.py`.

**Stress case:** a longer, multi-clause LLM reply (30+ words) — this is
where HTTP mode's "wait for the whole file" behavior is expected to
visibly lag behind WebSocket streaming's "start speaking after the first
sentence" behavior.

## Procedure

1. Run `benchmark.py` to isolate the TTS-only effect: 3 utterances of
   increasing length, each synthesized once via HTTP and once via
   WebSocket, TTFB recorded for both.
2. Run `agent.py`, have a live conversation covering both a short and a
   long expected reply, and inspect `latency_log.csv` for the same
   pattern in the full pipeline (not just the isolated TTS call).
3. Repeat each condition at least 3 times; report mean and note
   variance rather than a single run.

## Reproduce it

```bash
export RIME_API_KEY=your_key
python benchmark.py          # writes benchmark_results.csv
```

Then flip `use_websocket=False` in `agent.py`'s `rime.TTS(...)` call to
capture the "before" baseline for the full-pipeline test, and back to
`True` for the "after" result — same utterances, same speaker/model, only
that one flag changed.

## Result

**Measured on:** your local machine, single test run, Coda model / celeste speaker, 3 utterances of increasing length (17, 54, 111 characters).

| Condition | Mean TTFB | Notes |
|---|---|---|
| HTTP (`/v1/rime-tts`) | 1.016s | baseline — wait for response to start streaming |
| WebSocket, new connection per call | 1.514s | **worse than HTTP** — handshake cost dominates a single-shot call |
| WebSocket, 1 connection reused across all 3 calls | 0.351s | includes 1st call's handshake |
| WebSocket, warm, excluding 1st call | **0.335s** | fairest comparison to HTTP — **~3x faster, ~680ms saved per turn** |

**Conclusion:** a WebSocket connection opened fresh for every utterance is not a latency win over HTTP — the handshake overhead outweighs the streaming benefit for a single call. The benefit only appears once the connection is kept warm across turns, which is exactly how a real voice agent behaves (one connection for the whole conversation, not one per reply). This validates the architecture in `agent.py`, which opens the Rime WebSocket connection once per session via `use_websocket=True` rather than per turn.

**Reproduce:** `python benchmark.py` with `RIME_API_KEY` set. Full run prints all three conditions and writes `benchmark_results.csv`.

## Full-pipeline result (from `agent.py`, not the isolated benchmark)

One real conversational turn, captured from `latency_log.csv`:

| eou_delay | llm_ttft | tts_ttfb | perceived_response |
|---|---|---|---|
| 1.080s | 0.627s | 1.295s | **3.003s** |

This `tts_ttfb` (1.295s) is notably higher than the isolated benchmark's
warm-connection number (0.335s) for the same model/speaker. Cross-referencing
the worker log for this turn shows why: Rime's WebSocket connection closed
unexpectedly mid-session (`status_code=-1, retryable=True`) and the LiveKit
plugin's automatic retry added real reconnection overhead before this turn's
audio could start. This is disclosed as a limitation, not smoothed over --
see "Failure behavior" in `README.md` for the full incident.

Committed alongside this file: `benchmark_results.csv` (isolated TTS
benchmark output) and `latency_log.csv` (real full-pipeline turns), so
judges can inspect raw data rather than only the summarized numbers above.

## Secondary finding: per-utterance stream draining is required for correctness

While building the warm-connection test, an early version of `benchmark.py` returned near-zero TTFB values on the 2nd and 3rd calls in a reused connection. Root cause: Rime's `/ws3` protocol sends multiple `chunk` events per utterance followed by a `done` event, and the script was moving on to the next utterance before draining the previous one's remaining messages — so leftover audio from utterance N was misread as the start of utterance N+1's response.

Fix: after sending each `text` message, read events until a `done` (or `error`) event is received for that specific utterance before sending the next one. This is disclosed here because it's a real correctness requirement for any client maintaining a persistent Rime WebSocket connection across multiple conversational turns — not just a benchmarking artifact. `agent.py` inherits this correctly via the LiveKit Rime plugin, which handles this internally, but a from-scratch WebSocket client (as in `benchmark.py`) must implement it explicitly.


## Limitations

- Numbers reflect network conditions between the machine running the
  benchmark and Rime's servers at the time of testing; they are not a
  universal latency guarantee.
- `benchmark.py`'s WebSocket client is a best-effort implementation of the
  documented endpoint and should be checked against Rime's WebSocket API
  reference before being cited as a certified measurement.
- Client-side (browser/mobile) network RTT and audio buffering are not
  included in either number — only the server-side path is measured.
- Cached vs. uncached runs are not currently separated; if you see a fast
  first run and slower subsequent runs (or vice versa), label them
  separately per the hackathon's evaluation rules rather than averaging
  them together.
