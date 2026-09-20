"""
benchmark.py — Rime TTS latency benchmark (HTTP vs WebSocket)

Measures "time to first audio byte" (TTFB): the delay between sending a
synthesis request and receiving the first playable audio chunk back.
This is the TTS leg of the "perceived response time" acceptance test:

    perceived_response_time = eou_delay + llm_ttft + tts_ttfb + playback_start

Isolating this leg proves (or disproves) that switching Rime to streaming
mode actually reduces the delay the user experiences, independent of
whatever STT/LLM you pair it with.

Usage:
    export RIME_API_KEY="your_api_key_here"
    pip install requests websockets
    python benchmark.py

Output:
    - Printed table of per-utterance TTFB for HTTP and WebSocket modes
    - benchmark_results.csv with raw data for reproducibility

NOTE ON THE WEBSOCKET PROTOCOL:
Confirmed against Rime's WebSocket API reference (docs.rime.ai/docs/websockets
and /api-reference/coda/websockets-json): use the /ws3 endpoint, pass speaker/
modelId/audioFormat as URL query params, send {"text": "..."} as JSON, and
read back JSON messages where {"type": "chunk", "data": <base64 audio>}.
"""
import asyncio
import csv
import json
import os
import statistics
import time
from pathlib import Path

import requests
import websockets

RIME_API_KEY = os.environ.get("RIME_API_KEY")
if not RIME_API_KEY:
    raise SystemExit("Set RIME_API_KEY before running this benchmark.")

HTTP_URL = "https://users.rime.ai/v1/rime-tts"
WS_URL = "wss://users-ws.rime.ai/ws3"  # Rime's flagship JSON WebSocket endpoint

MODEL_ID = "coda"     # matches the voice used in agent.py
SPEAKER = "celeste"   # switched from lyra per user preference
LANG = "eng"

# Vary length deliberately: perceived latency work should show TTFB stays
# flat as text gets longer (streaming) vs. growing with length (non-streaming).
TEST_UTTERANCES = [
    "Sure, one moment.",
    "Your appointment is confirmed for Tuesday at three PM.",
    "I found four matching results, here is the first one, and I can read "
    "the rest if you would like me to continue.",
]

RESULTS_CSV = Path("benchmark_results.csv")


def http_ttfb(text: str) -> float:
    """Time to first byte using Rime's standard HTTP endpoint."""
    headers = {
        "Accept": "audio/wav",
        "Authorization": f"Bearer {RIME_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"text": text, "speaker": SPEAKER, "modelId": MODEL_ID, "lang": LANG}

    start = time.perf_counter()
    with requests.post(HTTP_URL, headers=headers, json=payload, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=1024):
            if chunk:
                return time.perf_counter() - start
    return float("nan")


async def ws_ttfb(text: str) -> float:
    """Time to first audio chunk using Rime's /ws3 JSON WebSocket endpoint.

    Per Rime's WebSocket API docs: synthesis parameters (speaker, modelId,
    audioFormat) are passed as URL query params at connect time, not in the
    message body. Messages sent/received are JSON; audio arrives as
    base64-encoded "chunk" messages, e.g. {"type": "chunk", "data": "..."}.

    NOTE: this opens a fresh connection per call, so the result includes
    one full TCP+TLS+WebSocket handshake. That's the fair, honest number
    for "cold" / first-turn latency, but it is NOT representative of a
    real voice agent that keeps one connection open across a whole
    conversation. See ws_ttfb_warm() below for that scenario.
    """
    url = f"{WS_URL}?speaker={SPEAKER}&modelId={MODEL_ID}&audioFormat=mp3&lang={LANG}"
    headers = [("Authorization", f"Bearer {RIME_API_KEY}")]
    start = time.perf_counter()
    first_chunk_time = None
    async with websockets.connect(url, additional_headers=headers) as ws:
        await ws.send(json.dumps({"text": text}))
        async for raw in ws:
            message = json.loads(raw)
            mtype = message.get("type")
            if mtype == "chunk" and first_chunk_time is None:
                first_chunk_time = time.perf_counter() - start
            elif mtype == "error":
                raise RuntimeError(f"Rime WS error: {message.get('message')}")
            if mtype == "done":
                break
    return first_chunk_time if first_chunk_time is not None else float("nan")


async def ws_ttfb_warm(texts: list[str]) -> list[float]:
    """Time to first audio chunk for each utterance, reusing ONE already-open
    WebSocket connection across all of them. This is the fair comparison
    point against HTTP for a real voice agent, where the connection is
    opened once at session start and reused for every conversational turn
    -- the handshake cost is paid once, not per utterance.

    IMPORTANT: Rime's /ws3 protocol sends multiple "chunk" events per
    utterance, followed by a "done" event marking that utterance's audio
    as complete. Each utterance's message stream MUST be fully drained
    (read through "done") before sending the next "text" message --
    otherwise leftover chunks from the previous utterance are consumed as
    if they belonged to the next one, producing bogus near-zero TTFB.

    Returns one TTFB per text, in order.
    """
    url = f"{WS_URL}?speaker={SPEAKER}&modelId={MODEL_ID}&audioFormat=mp3&lang={LANG}"
    headers = [("Authorization", f"Bearer {RIME_API_KEY}")]
    results = []
    async with websockets.connect(url, additional_headers=headers) as ws:
        for text in texts:
            start = time.perf_counter()
            first_chunk_time = None
            await ws.send(json.dumps({"text": text}))
            async for raw in ws:
                message = json.loads(raw)
                mtype = message.get("type")
                if mtype == "chunk" and first_chunk_time is None:
                    first_chunk_time = time.perf_counter() - start
                elif mtype == "error":
                    raise RuntimeError(f"Rime WS error: {message.get('message')}")
                if mtype == "done":
                    break  # this utterance's stream is fully drained
            results.append(first_chunk_time if first_chunk_time is not None else float("nan"))
    return results


def run():
    rows = []
    print(f"{'mode':<10}{'chars':<8}{'ttfb_s':<10}text")
    for text in TEST_UTTERANCES:
        try:
            http_t = http_ttfb(text)
        except Exception as exc:
            print(f"HTTP request failed for {text!r}: {exc}")
            http_t = float("nan")
        rows.append(("http", len(text), http_t, text))
        print(f"{'http':<10}{len(text):<8}{http_t:<10.3f}{text[:40]}")

        try:
            ws_t = asyncio.run(ws_ttfb(text))
        except Exception as exc:
            print(f"WebSocket (cold) request failed for {text!r}: {exc}")
            ws_t = float("nan")
        rows.append(("websocket_cold", len(text), ws_t, text))
        print(f"{'ws_cold':<10}{len(text):<8}{ws_t:<10.3f}{text[:40]}")

    # Warm run: one connection, reused across all utterances -- this is
    # the number that's actually comparable to HTTP for a live agent.
    print("\n--- WebSocket, warm connection (one connect, N sends) ---")
    try:
        warm_results = asyncio.run(ws_ttfb_warm(TEST_UTTERANCES))
    except Exception as exc:
        print(f"Warm WebSocket run failed: {exc}")
        warm_results = [float("nan")] * len(TEST_UTTERANCES)

    for text, t in zip(TEST_UTTERANCES, warm_results):
        rows.append(("websocket_warm", len(text), t, text))
        print(f"{'ws_warm':<10}{len(text):<8}{t:<10.3f}{text[:40]}")

    with RESULTS_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mode", "char_count", "ttfb_seconds", "text"])
        w.writerows(rows)

    def _mean(mode):
        vals = [r[2] for r in rows if r[0] == mode and r[2] == r[2]]  # drop NaN
        return statistics.mean(vals) if vals else float("nan"), len(vals)

    http_mean, http_n = _mean("http")
    cold_mean, cold_n = _mean("websocket_cold")
    warm_mean, warm_n = _mean("websocket_warm")
    # "Warm" here means turns 2..N on a reused connection -- turn 1 still
    # pays handshake cost, so exclude it for the cleanest apples-to-apples read.
    warm_after_first = [r[2] for r in rows if r[0] == "websocket_warm"][1:]
    warm_after_first = [v for v in warm_after_first if v == v]

    print("\n--- Summary ---")
    print(f"HTTP                        mean TTFB: {http_mean:.3f}s over {http_n} calls")
    print(f"WebSocket (new conn/call)   mean TTFB: {cold_mean:.3f}s over {cold_n} calls")
    print(f"WebSocket (1 conn, all calls) mean TTFB: {warm_mean:.3f}s over {warm_n} calls")
    if warm_after_first:
        print(f"WebSocket (warm, excl. 1st call) mean TTFB: {statistics.mean(warm_after_first):.3f}s "
              f"over {len(warm_after_first)} calls  <-- fairest comparison to HTTP")
    print(f"Raw results written to {RESULTS_CSV}")


if __name__ == "__main__":
    run()
