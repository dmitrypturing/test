#!/usr/bin/env python3
"""merge_parts.py — one meeting recorded in several files -> one set of artifacts.
Works on Windows, macOS and Linux (needs ffprobe; the macOS-only twin is merge_parts.sh).

    python tools/merge_parts.py "Team meeting 20.08.2026" part1.m4a part2.m4a [...]

Every part must already be transcribed (transcribe.py does that; each part keeps
its own .srt/.txt for jumping into that audio file). This stitches them into
<name>.srt / .txt / .transcript.md next to the first part, on one continuous
timeline: part 2 timestamps = position in part 2 + length of part 1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def which(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    for p in sorted((ROOT / "bin").rglob(exe)) if (ROOT / "bin").is_dir() else []:
        if p.is_file():
            return str(p)
    found = shutil.which(name)
    if not found:
        sys.exit(f"merge_parts: {name} not found")
    return found


def duration(ffprobe: str, p: Path) -> float:
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        sys.exit(f"merge_parts: could not read the duration of {p}")


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: merge_parts.py <output-name> <part1> <part2> [...]", file=sys.stderr)
        return 1
    name, parts = sys.argv[1], [Path(p).resolve() for p in sys.argv[2:]]
    ffprobe = which("ffprobe")
    py = sys.executable
    outdir = parts[0].parent
    work = Path(tempfile.mkdtemp(prefix="merge_parts."))
    try:
        manifest, offset, starts = [], 0.0, []
        for i, p in enumerate(parts):
            srt = p.with_name(p.stem + ".srt")
            if not srt.exists():
                sys.exit(f"merge_parts: {srt.name} not found — run transcribe.py first")
            dur = duration(ffprobe, p)
            shutil.copy(srt, work / f"chunk_{i:03d}.srt")
            starts.append(offset)
            manifest.append({"index": i, "wav": f"chunk_{i:03d}.wav",
                             "offset_ms": round(offset * 1000), "duration_ms": round(dur * 1000)})
            offset += dur
        (work / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8")
        total = round(offset)

        out_prefix = outdir / name
        subprocess.run([py, "-I", str(HERE / "chunker.py"), "merge", str(work), "--out", str(out_prefix)], check=True)

        label = " · ".join(f"part {i + 1} starts at [{hms(s)}]" for i, s in enumerate(starts))
        subprocess.run([py, "-I", str(HERE / "srt_to_md.py"), f"{out_prefix}.srt", "--title", name,
                        "--audio", label, "--out", f"{out_prefix}.transcript.md"], check=True)

        kept = 0
        for p in parts:
            log = p.with_name(p.stem + ".transcribe.log")
            if log.exists():
                kept += sum(int(m) for m in re.findall(r"Reduced audio from \d+ to (\d+)",
                                                       log.read_text(encoding="utf-8", errors="replace")))
        qc = [py, "-I", str(HERE / "qc.py"), f"{out_prefix}.srt", "--duration", str(total)]
        if kept:
            qc += ["--speech", f"{kept / 16000:.1f}"]
        subprocess.run(qc)
        print(f"merged -> {out_prefix}.srt / .txt / .transcript.md")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
