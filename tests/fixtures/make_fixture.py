"""Generate the legal test fixture: a 96-second synthetic "stream" with four planted reaction events.

Everything is created locally: speech is macOS text-to-speech (`say`), screams/impacts/laughter are synthesized noise and tones,
video is ffmpeg lavfi test sources. No third-party footage. The script and the resulting file are dedicated to the public domain (CC0).

    python tests/fixtures/make_fixture.py OUTPUT.mp4

Ground truth (EVENTS) is exported so tests can assert the pipeline found the planted moments.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

SR = 16000
DURATION = 96.0
CALM = "Daniel"
LOUD = "Samantha"

# (start_sec, voice, text, gain)
LINES = [
    (1.0, CALM, "Alright chat, welcome back to the stream. Today we are playing the haunted house game.", 1.0),
    (9.0, CALM, "I think this hallway is totally empty, nothing could possibly go wrong here.", 1.0),
    (17.0, LOUD, "Oh no! Oh no no no no! Get it off me! Get it off me!", 3.0),
    (26.0, CALM, "Okay okay, that was nothing, I am completely fine.", 1.0),
    (33.0, CALM, "Chat says there is a secret door behind the painting, let me check it out.", 1.0),
    (40.6, LOUD, "What is that? Is that a ghost? It is huge! Run run run!", 3.0),
    (50.0, CALM, "This game has terrible controls honestly, I keep walking into the walls.", 1.0),
    (58.0, CALM, "I will open this chest very carefully, step by step.", 1.0),
    (62.0, LOUD, "Noooo! It exploded! Why does it always explode?", 3.0),
    (72.0, CALM, "Let me check my health bar and the inventory for a second.", 1.0),
    (81.5, LOUD, "Oh my god! The floor is gone! The floor is gone!", 3.0),
    (90.0, CALM, "Anyway, that is enough for today, see you next time chat.", 1.0),
]
# Ground truth of the planted reaction moments: (impact time, laughter start, laughter end)
EVENTS = [(17.0, 21.5, 24.0), (40.6, 44.5, 46.8), (62.0, 66.0, 69.0), (81.5, 85.5, 88.0)]
HARD_CUTS = [17.2, 24.5, 40.8, 47.5, 62.2, 69.5, 81.7, 89.0]      # video scene changes land just after each impact (a jump-scare cut), not before the held beat


def say(voice: str, text: str, tmp: Path, idx: int) -> np.ndarray:
    aiff = tmp / f"s{idx}.aiff"
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(aiff), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def build_audio(tmp: Path) -> np.ndarray:
    rng = np.random.default_rng(7)
    n = int(DURATION * SR)
    mix = rng.normal(0, 0.002, n).astype(np.float32)                              # faint room noise: the floor is never digital silence
    for i, (t0, voice, text, gain) in enumerate(LINES):
        x = say(voice, text, tmp, i)
        x = x / (np.abs(x).max() + 1e-9) * (0.9 if gain > 1 else 0.28)             # shouted lines are loud, narration is conversational
        a = int(t0 * SR)
        mix[a:a + len(x)] += x[: n - a]
    for t_imp, l0, l1 in EVENTS:
        a = int(t_imp * SR)
        k = np.arange(int(0.5 * SR)) / SR
        mix[a:a + len(k)] += (np.sin(2 * np.pi * 70 * k) * np.exp(-k * 9) * 0.8).astype(np.float32)            # impact thump
        m = int((l1 - l0) * SR)
        t = np.arange(m) / SR
        burst = (rng.normal(0, 1, m) * (0.5 + 0.5 * np.sin(2 * np.pi * 5.5 * t)) ** 2 * 0.35).astype(np.float32)   # laughter-like: bursty noise
        mix[int(l0 * SR):int(l0 * SR) + m] += burst
    return np.clip(mix, -0.98, 0.98)


def build(out: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="clipfixture-") as td:
        tmp = Path(td)
        audio = build_audio(tmp)
        wav = tmp / "a.f32"
        wav.write_bytes(audio.tobytes())
        edges = [0.0] + HARD_CUTS + [DURATION]
        sources = ["testsrc2=size=640x360:rate=30", "mandelbrot=size=640x360:rate=30", "gradients=size=640x360:rate=30:speed=0.05", "life=size=640x360:rate=30:mold=10:ratio=0.1:death_color=#003060:life_color=#ffd040"]
        inputs, chain = [], []
        for i in range(len(edges) - 1):
            d = edges[i + 1] - edges[i]
            inputs += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", sources[i % 2 if i % 2 == 0 else 1 + (i // 2) % 3]]
            chain.append(f"[{i}:v]setsar=1,format=yuv420p[v{i}]")
        k = len(edges) - 1
        inputs += ["-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(wav)]
        graph = ";".join(chain) + ";" + "".join(f"[v{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=0[v]"
        cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-map", f"{k}:a", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-t", f"{DURATION}", "-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr[-800:])
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]))
    print(sys.argv[1])
