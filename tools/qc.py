#!/usr/bin/env python3
"""Post-transcription sanity checks.

Reads the finished .srt and reports the things that silently went wrong last
time so nobody has to discover them by reading a 2-hour transcript:

  - repetition loops   (a run of identical consecutive cues = audio NOT transcribed)
  - coverage           (does the last cue reach the end of the recording?)
  - dead air           (gaps between cues long enough to mean lost or skipped speech)
  - density            (suspiciously few words per minute of audio)

Exit code 1 when anything is worth a human look; the messages say where.

    qc.py meeting.srt --duration 9906
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")

LOOP_RUN = 8          # consecutive identical cues that mean a recogniser loop
GAP_SEC = 300.0       # a hole this long is worth a look (VAD skips real silence)
TAIL_SEC = 120.0      # last cue should land within this of the end
MIN_WPM = 40.0        # per minute of audio — used only when speech time is unknown
MIN_SPEECH_WPM = 80.0 # per minute of detected speech; 8 meetings measured 102-122


def to_s(t: str) -> float:
    h, m, s, ms = TIME.match(t.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").lstrip("﻿").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start, _, end = lines[0].partition("-->")
        try:
            t0, t1 = to_s(start), to_s(end)
        except AttributeError:
            continue
        body = " ".join(ln.strip() for ln in lines[1:]).strip()
        if body:
            cues.append((t0, t1, body))
    return cues


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("srt", type=Path)
    ap.add_argument("--duration", type=float, default=0.0, help="audio duration in seconds")
    ap.add_argument("--speech", type=float, default=0.0,
                    help="seconds of audio the VAD kept as speech (from the whisper log)")
    args = ap.parse_args()

    cues = parse_srt(args.srt.read_text(encoding="utf-8", errors="replace"))
    if not cues:
        print("qc: FAIL — no cues at all; the transcription produced nothing")
        return 1

    warnings = []

    # 1. loops: runs of identical consecutive cues
    i = 0
    while i < len(cues):
        key = cues[i][2].strip().lower()
        j = i + 1
        while j < len(cues) and cues[j][2].strip().lower() == key:
            j += 1
        if j - i >= LOOP_RUN:
            warnings.append(
                f"recogniser loop: one line repeated {j - i}× from [{hms(cues[i][0])}] "
                f"to [{hms(cues[j - 1][1])}] — that stretch of audio was NOT transcribed")
        i = j

    # 2. holes between consecutive cues
    for (a0, a1, _), (b0, b1, _) in zip(cues, cues[1:]):
        if b0 - a1 >= GAP_SEC:
            warnings.append(
                f"gap: nothing transcribed between [{hms(a1)}] and [{hms(b0)}] "
                f"({(b0 - a1) / 60:.0f} min) — silence, a break, or lost speech")

    # 3. coverage of the tail
    if args.duration > 0 and cues[-1][1] < args.duration - TAIL_SEC:
        warnings.append(
            f"coverage: last cue ends at [{hms(cues[-1][1])}] but the audio runs "
            f"to [{hms(args.duration)}] — the tail is missing")

    # 4. density. Measured against detected speech when the VAD reports it: with
    # VAD on, a quiet stretch is folded inside one long cue, so a demo-heavy meeting
    # looks thin per minute of audio while its speech was decoded in full.
    minutes = (args.duration or cues[-1][1]) / 60
    words = sum(len(c[2].split()) for c in cues)
    if args.speech > 0:
        speech_min = args.speech / 60
        if speech_min > 3 and words / speech_min < MIN_SPEECH_WPM:
            warnings.append(
                f"density: only {words / speech_min:.0f} words per minute of detected speech "
                f"(normal here: 100–120) — speech was heard but not transcribed")
    elif minutes > 5 and words / minutes < MIN_WPM:
        warnings.append(
            f"density: only {words / minutes:.0f} words/min over {minutes:.0f} min — "
            f"a normal meeting runs 100+; much of the audio may be untranscribed")

    if warnings:
        for w in warnings:
            print(f"qc: WARNING — {w}")
        print(f"qc: {len(warnings)} issue(s) — check these against the audio before trusting the transcript")
        return 1

    speech = ""
    if args.speech > 0 and args.duration > 0:
        speech = f", {words / (args.speech / 60):.0f} wpm of speech (speech {args.speech / args.duration * 100:.0f}% of audio)"
    print(f"qc: OK — {len(cues)} cues, {words} words, covered to [{hms(cues[-1][1])}]{speech}, no loops, no gaps ≥ {GAP_SEC / 60:.0f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
