"""
agent.py — Low-latency voice assistant with per-turn latency logging

Pipeline: LiveKit transport -> Silero VAD -> Groq STT (Whisper) -> Groq LLM
          -> Rime TTS (WebSocket streaming) -> LiveKit playback

Groq is used for STT and LLM because it has a genuinely free API tier
(unlike OpenAI, which requires prepaid billing) -- this keeps the only
paid dependency in this project scoped to Rime, which is what's actually
being judged. Swap back to openai.STT()/openai.LLM() below if you'd
rather use OpenAI; the rest of the pipeline is unaffected either way.

This instruments the "perceived response time" acceptance test end to end
using LiveKit Agents' built-in metrics events:

    end_of_utterance_delay  (VAD/STT: user stops talking -> transcript final)
  + llm_ttft                (LLM: prompt sent -> first token)
  + tts_ttfb                (TTS: first token/sentence sent -> first audio byte)
  = perceived_response_time (approximation of user-felt latency;
                              does not include network RTT to the client
                              or client-side audio buffering, which vary
                              per device and should be measured separately
                              for a browser/mobile deployment)

Every turn is appended to latency_log.csv so results are reproducible and
inspectable after the fact, per the hackathon's evidence requirement.

Run:
    export RIME_API_KEY=...      export GROQ_API_KEY=...
    export LIVEKIT_URL=...       export LIVEKIT_API_KEY=...
    export LIVEKIT_API_SECRET=...
    python agent.py dev
"""
import csv
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    MetricsCollectedEvent,
    RoomInputOptions,
    WorkerOptions,
    cli,
    metrics,
)
from livekit.plugins import groq, rime, silero

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("latency-agent")

METRICS_LOG_PATH = Path("latency_log.csv")


def _ensure_log_header() -> None:
    if not METRICS_LOG_PATH.exists():
        with METRICS_LOG_PATH.open("w", newline="") as f:
            csv.writer(f).writerow(
                [
                    "unix_time",
                    "turn_id",
                    "eou_delay_s",
                    "llm_ttft_s",
                    "tts_ttfb_s",
                    "perceived_response_s",
                ]
            )


@dataclass
class TurnTimers:
    eou_delay: float = 0.0
    llm_ttft: float = 0.0
    tts_ttfb: float = 0.0


class LatencyAssistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "Your name is Kira, a hands-free voice assistant for field "
                "workers and drivers -- people whose hands and eyes are busy "
                "and who cannot look at a screen. If asked your name, say "
                "Kira. Answer in one or two short sentences: your users are "
                "driving or working, so brevity is a safety requirement, not "
                "just a latency optimization. Sound calm and direct, like a "
                "co-pilot relaying quick information, not a chatty assistant."
            )
        )


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    _ensure_log_header()

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=groq.STT(model="whisper-large-v3-turbo", language="en"),
        llm=groq.LLM(model="openai/gpt-oss-120b"),  # llama-3.3-70b-versatile was deprecated by Groq on 2026-06-17
        tts=rime.TTS(
            model="coda",
            speaker="celeste",
            lang="eng",
            use_websocket=True,   # streaming mode -- required for this acceptance test;
                                  # flip to False to reproduce the "before" HTTP baseline
        ),
    )

    turn_timers = TurnTimers()
    turn_counter = {"n": 0}

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent) -> None:
        m = ev.metrics

        # NOTE: exact class/field names (EOUMetrics.end_of_utterance_delay,
        # LLMMetrics.ttft, TTSMetrics.ttfb) reflect the livekit-agents metrics
        # API at the time this was written. Pin your livekit-agents version
        # in requirements.txt and confirm these field names against
        # `python -c "from livekit.agents import metrics; help(metrics)"`
        # before relying on this for the submitted evidence.
        if isinstance(m, metrics.EOUMetrics):
            turn_timers.eou_delay = m.end_of_utterance_delay
        elif isinstance(m, metrics.LLMMetrics):
            turn_timers.llm_ttft = m.ttft
        elif isinstance(m, metrics.TTSMetrics):
            turn_timers.tts_ttfb = m.ttfb
            # TTS metrics land last in a turn's causal chain -> log here.
            turn_counter["n"] += 1
            perceived = turn_timers.eou_delay + turn_timers.llm_ttft + turn_timers.tts_ttfb
            logger.info(
                "turn=%d eou=%.3fs llm_ttft=%.3fs tts_ttfb=%.3fs perceived_response=%.3fs",
                turn_counter["n"],
                turn_timers.eou_delay,
                turn_timers.llm_ttft,
                turn_timers.tts_ttfb,
                perceived,
            )
            with METRICS_LOG_PATH.open("a", newline="") as f:
                csv.writer(f).writerow(
                    [
                        time.time(),
                        turn_counter["n"],
                        f"{turn_timers.eou_delay:.3f}",
                        f"{turn_timers.llm_ttft:.3f}",
                        f"{turn_timers.tts_ttfb:.3f}",
                        f"{perceived:.3f}",
                    ]
                )
            turn_timers.eou_delay = turn_timers.llm_ttft = turn_timers.tts_ttfb = 0.0

    await session.start(
        room=ctx.room,
        agent=LatencyAssistant(),
        room_input_options=RoomInputOptions(),
    )

    await session.generate_reply(
        instructions="Greet the user as Kira in one short sentence and ask how you can help."
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
