#!/usr/bin/env python3
"""Turn a whisper .srt into timestamped paragraphs.

whisper emits one cue every few seconds, which is right for subtitles and wrong
for reading. This merges cues into paragraphs that break on real pauses, and
prefixes each with the time it starts, so a summary can point back at the audio:

    **[0:12:40]** Обсудили представление модели …

Usage:
    srt_to_md.py meeting.srt --out meeting.transcript.md [--title T] [--audio A]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# a paragraph is closed when it gets this long …
MAX_PARAGRAPH_SEC = 90.0
# … or when a pause this long follows a sentence that has already run a while
PAUSE_SEC = 1.2
MIN_PARAGRAPH_SEC = 25.0
# non-speech markers whisper sometimes emits on music/noise
NOISE = re.compile(r"^[\[(\*][^\])\*]{0,40}[\])\*]$")
SENTENCE_END = ".!?…»\"'"

CueList = list[tuple[float, float, str]]


def parse_time(t: str) -> float:
    h, m, rest = t.split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def parse_srt(text: str) -> CueList:
    cues: CueList = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").lstrip("﻿").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if "-->" not in lines[0]:
            continue
        start, _, end = lines[0].partition("-->")
        try:
            t0, t1 = parse_time(start.strip()), parse_time(end.strip())
        except ValueError:
            continue
        body = " ".join(ln.strip() for ln in lines[1:]).strip()
        if body and not NOISE.match(body):
            cues.append((t0, t1, body))
    return cues


def collapse_loops(cues: CueList, limit: int = 3) -> tuple[CueList, int]:
    """whisper can get stuck repeating one line over a long silence. Keep a
    couple of copies, mark the rest, so the loop stops drowning the transcript."""
    out: CueList = []
    collapsed = 0
    i = 0
    while i < len(cues):
        key = cues[i][2].strip().lower()
        j = i + 1
        while j < len(cues) and cues[j][2].strip().lower() == key:
            j += 1
        run = j - i
        if run > limit:
            out.extend(cues[i : i + 2])
            t0, t1, _ = cues[j - 1]
            out.append((t0, t1, f"[same line repeated {run}× — likely a recogniser loop]"))
            collapsed += 1
        else:
            out.extend(cues[i:j])
        i = j
    return out, collapsed


def paragraphs(cues: CueList) -> list[tuple[float, str]]:
    paras: list[tuple[float, str]] = []
    buf: list[str] = []
    start = end = 0.0

    def flush() -> None:
        if buf:
            paras.append((start, " ".join(buf).strip()))
            buf.clear()

    for t0, t1, body in cues:
        if not buf:
            start = t0
        else:
            gap = t0 - end
            span = end - start
            if span >= MAX_PARAGRAPH_SEC or (gap >= PAUSE_SEC and span >= MIN_PARAGRAPH_SEC
                                             and buf[-1].rstrip()[-1:] in SENTENCE_END):
                flush()
                start = t0
        buf.append(body)
        end = t1
    flush()
    return paras


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("srt", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--title", default=None)
    ap.add_argument("--audio", default=None)
    args = ap.parse_args()

    cues = parse_srt(args.srt.read_text(encoding="utf-8", errors="replace"))
    if not cues:
        print(f"{args.srt}: no cues found", file=sys.stderr)
        return 1

    cues, loops = collapse_loops(cues)
    paras = paragraphs(cues)
    words = sum(len(p.split()) for _, p in paras)
    duration = cues[-1][1]

    head = [f"# {args.title or args.srt.stem}", ""]
    meta = [f"Transcript · {hms(duration)} · ~{words:,} words".replace(",", " ")]
    if args.audio:
        meta.append(f"Source: `{args.audio}`")
    meta.append("Transcribed locally with whisper.cpp — machine output, expect errors in names and terms.")
    if loops:
        meta.append(f"{loops} recogniser loop(s) collapsed.")
    head += ["  \n".join(meta), "", "Timestamps are hours:minutes:seconds from the start of the recording.", "", "---", ""]

    body = [f"**[{hms(t)}]** {text}" for t, text in paras]
    args.out.write_text("\n".join(head) + "\n" + "\n\n".join(body) + "\n", encoding="utf-8")
    print(f"{args.out.name}: {len(paras)} paragraphs, {words} words")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
