# RIME_PROJECT
Voice assistant for hands-free users (drivers, field workers) built with LiveKit + Groq + Rime TTS. Core finding: streaming Rime's voice output over a kept-open WebSocket cuts response delay ~3x vs. reopening it each time. Currently self-hosted; working on a way to let others test it without needing my laptop running.
