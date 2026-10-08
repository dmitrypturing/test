#!/usr/bin/env python3
"""Split a long recording into chunks, and stitch the results back together.

whisper-cli writes nothing until it finishes, so an interrupted three-hour run
loses everything. Transcribing in chunks makes the work resumable: each chunk
that already has an .srt is skipped on the next run.

Chunks are not cut on a stopwatch — the boundary moves to the quietest moment
near the target time, so it lands in a pause instead of the middle of a word.

    chunker.py split  audio.wav workdir/ [--minutes 15]
    chunker.py merge  workdir/ --out /path/to/name    # writes name.srt and name.txt
"""

from __future__ import annotations

import argparse
import array
import json
import re
import sys
import wave
from pathlib import Path

SEARCH_SEC = 45.0     # how far from the target boundary we may move the cut
WINDOW_SEC = 1.0      # length of the quiet window we look for
STRIDE = 8            # subsample when scanning for silence; plenty for finding a pause


def quietest_offset(frames: array.array, rate: int, centre: int, reach: int) -> int:
    """Index of the quietest WINDOW_SEC window within `reach` samples of `centre`."""
    lo = max(0, centre - reach)
    hi = min(len(frames), centre + reach)
    win = int(WINDOW_SEC * rate)
    if hi - lo <= win:
        return centre
    best, best_energy = centre, None
    hop = max(1, win // 4)
    for start in range(lo, hi - win, hop):
        seg = frames[start : start + win : STRIDE]
        energy = sum(v * v for v in seg)
        if best_energy is None or energy < best_energy:
            best, best_energy = start + win // 2, energy
    return best


def split(audio: Path, workdir: Path, minutes: float) -> int:
    with wave.open(str(audio)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            sys.exit("chunker: expected 16-bit mono PCM")
        rate, total = w.getframerate(), w.getnframes()
        frames = array.array("h")
        frames.frombytes(w.readframes(total))

    workdir.mkdir(parents=True, exist_ok=True)
    target = int(minutes * 60 * rate)
    reach = int(SEARCH_SEC * rate)

    cuts = [0]
    while cuts[-1] + target < total:
        nominal = cuts[-1] + target
        # don't bother searching if the tail is short — just run to the end
        if total - nominal < reach:
            break
        cuts.append(quietest_offset(frames, rate, nominal, reach))
    cuts.append(total)

    manifest = []
    for i in range(len(cuts) - 1):
        start, end = cuts[i], cuts[i + 1]
        path = workdir / f"chunk_{i:03d}.wav"
        if not path.exists():
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)
                out.writeframes(frames[start:end].tobytes())
        manifest.append({
            "index": i,
            "wav": path.name,
            "offset_ms": round(start / rate * 1000),
            "duration_ms": round((end - start) / rate * 1000),
        })

    (workdir / "chunks.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"{len(manifest)} chunks of ~{minutes:g} min in {workdir.name}/")
    return 0


# ------------------------------------------------------------------- merge --

TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def to_ms(t: str) -> int:
    h, m, s, ms = TIME.match(t.strip()).groups()
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))


def to_srt_time(ms: int) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def merge(workdir: Path, out_prefix: Path) -> int:
    manifest = json.loads((workdir / "chunks.json").read_text(encoding="utf-8"))
    cues: list[tuple[int, int, str]] = []
    missing = []

    for entry in manifest:
        srt = workdir / f"chunk_{entry['index']:03d}.srt"
        if not srt.exists():
            missing.append(entry["index"])
            continue
        offset = entry["offset_ms"]
        blocks = re.split(r"\n\s*\n", srt.read_text(encoding="utf-8", errors="replace").strip())
        for block in blocks:
            lines = [ln for ln in block.split("\n") if ln.strip()]
            if len(lines) < 2:
                continue
            if lines[0].strip().isdigit():
                lines = lines[1:]
            if "-->" not in lines[0]:
                continue
            start, _, end = lines[0].partition("-->")
            try:
                t0, t1 = to_ms(start) + offset, to_ms(end) + offset
            except AttributeError:
                continue
            body = " ".join(ln.strip() for ln in lines[1:]).strip()
            if body:
                cues.append((t0, t1, body))

    if missing:
        sys.exit(f"chunker: chunks not transcribed yet: {missing}")
    if not cues:
        sys.exit("chunker: no cues found in any chunk")

    cues.sort(key=lambda c: c[0])
    srt_out = []
    for n, (t0, t1, body) in enumerate(cues, 1):
        srt_out.append(f"{n}\n{to_srt_time(t0)} --> {to_srt_time(t1)}\n{body}\n")
    # not with_suffix(): a recording called "meeting 13.08.2026" has ".2026" as its
    # apparent extension, and with_suffix would replace it
    Path(f"{out_prefix}.srt").write_text("\n".join(srt_out), encoding="utf-8")
    Path(f"{out_prefix}.txt").write_text(
        "\n".join(body for _, _, body in cues) + "\n", encoding="utf-8")

    print(f"merged {len(manifest)} chunks → {len(cues)} cues")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("split")
    sp.add_argument("audio", type=Path)
    sp.add_argument("workdir", type=Path)
    sp.add_argument("--minutes", type=float, default=15.0)

    mp = sub.add_parser("merge")
    mp.add_argument("workdir", type=Path)
    mp.add_argument("--out", type=Path, required=True, help="output prefix, without extension")

    args = ap.parse_args()
    if args.cmd == "split":
        return split(args.audio, args.workdir, args.minutes)
    return merge(args.workdir, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
