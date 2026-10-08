#!/usr/bin/env bash
#
# merge_parts.sh — one meeting recorded in several files → one set of artifacts.
#
#   tools/merge_parts.sh "FxCAD meeting 20.08.2026" part1.m4a part2.m4a [...]
#
# Every part must already be transcribed (./transcribe.sh does that; each part
# keeps its own .srt/.txt for jumping into that audio file). This stitches them
# into <name>.srt / .txt / .transcript.md next to the first part, on one
# continuous timeline: part 2 timestamps = position in part 2 + length of part 1.
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ $# -ge 3 ] || { echo "usage: merge_parts.sh <output-name> <part1> <part2> [...]" >&2; exit 1; }
NAME="$1"; shift

OUTDIR="$(cd "$(dirname "$1")" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/merge_parts.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# stage each part's srt as a chunk with its cumulative offset; print starts + total
INFO=$(python3 - "$WORK" "$@" <<'PY'
import json, shutil, subprocess, sys
from pathlib import Path
work, parts = Path(sys.argv[1]), sys.argv[2:]
manifest, offset, starts = [], 0.0, []
for i, p in enumerate(parts):
    p = Path(p)
    srt = p.with_name(p.stem + ".srt")
    if not srt.exists():
        sys.exit(f"merge_parts: {srt.name} not found — run ./transcribe.sh first")
    out = subprocess.run(["afinfo", str(p)], capture_output=True, text=True).stdout
    dur = next(float(l.split(":")[1].split()[0]) for l in out.splitlines() if "estimated duration" in l)
    shutil.copy(srt, work / f"chunk_{i:03d}.srt")
    starts.append(offset)
    manifest.append({"index": i, "wav": f"chunk_{i:03d}.wav",
                     "offset_ms": round(offset * 1000), "duration_ms": round(dur * 1000)})
    offset += dur
(work / "chunks.json").write_text(json.dumps(manifest))
def hms(s): s = int(s); return f"{s//3600}:{(s%3600)//60:02d}:{s%60:02d}"
print("|".join(hms(s) for s in starts))
print(round(offset))
PY
)
STARTS=$(echo "$INFO" | head -1)
TOTAL=$(echo "$INFO" | tail -1)

python3 "$ROOT/tools/chunker.py" merge "$WORK" --out "$OUTDIR/$NAME"

LABEL=""
i=1
IFS='|' read -ra ARR <<< "$STARTS"
for s in "${ARR[@]}"; do LABEL="$LABEL· part $i starts at [$s] "; i=$((i+1)); done
LABEL="${LABEL#· }"
python3 "$ROOT/tools/srt_to_md.py" "$OUTDIR/$NAME.srt" \
  --title "$NAME" --audio "$LABEL" --out "$OUTDIR/$NAME.transcript.md"

SPEECH="$(for part in "$@"; do grep -o 'Reduced audio from [0-9]* to [0-9]*' "${part%.*}.transcribe.log" 2>/dev/null; done | awk '{s += $6} END {if (s) printf "%.1f", s/16000}')"
python3 "$ROOT/tools/qc.py" "$OUTDIR/$NAME.srt" --duration "$TOTAL" ${SPEECH:+--speech "$SPEECH"} || true
echo "merged → $OUTDIR/$NAME.{srt,txt,transcript.md}"
