#!/usr/bin/env python3
"""Benchmark CAM++ using live PCM from audio_service's input WebSocket."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from speaker_identity import SpeakerIdentityService  # noqa: E402


async def capture(url: str, milliseconds: int) -> bytes:
    chunks = []
    total = 0
    target = milliseconds * 16000 * 2 // 1000
    async with websockets.connect(url, max_size=None) as ws:
        while total < target:
            message = await ws.recv()
            if isinstance(message, bytes):
                chunks.append(message)
                total += len(message)
    return b"".join(chunks)[:target]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8093/ws/audio/input?delivery=continuous&pre_roll_ms=0")
    parser.add_argument("--audio-ms", type=int, default=3000)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--config", default=str(ROOT / "config" / "speaker.json"))
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    service = SpeakerIdentityService(config, root=ROOT)
    if not await service.start():
        raise SystemExit(json.dumps(service.status(), ensure_ascii=False))
    pcm = await capture(args.url, args.audio_ms)
    result = await service.benchmark(pcm, args.iterations)
    result["model"] = service.status()["model"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
