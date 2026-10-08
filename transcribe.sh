#!/usr/bin/env bash
#
# transcribe.sh — turn meeting recordings into text transcripts, locally.
#
#   1. drop a recording into  meetings/<YYYYMMDD>/<whatever>.m4a
#   2. run  ./transcribe.sh
#   3. next to each recording you get:
#
#        <name>.txt             plain text, no timestamps
#        <name>.srt             subtitles, for jumping back into the audio
#        <name>.transcript.md   timestamped paragraphs — the file to read and summarise
#
# Long recordings are transcribed in chunks, so an interrupted run resumes where
# it stopped instead of starting the hours over. Nothing is uploaded: whisper.cpp
# runs on the local GPU.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
transcribe.sh — meeting recordings to text, locally

  ./transcribe.sh                        every recording that lacks a transcript
  ./transcribe.sh 20260813               only that folder
  ./transcribe.sh "20260813/talk.m4a"    only that file

Options
  --list, --dry-run       show what would run, do nothing
  --force                 redo transcripts that already exist
  --model NAME            large-v3 (default) | large-v3-turbo | medium | small
  --lang CODE             ru (default), en, … or 'auto' to detect
  --threads N             decoding threads (default: performance cores)
  --chunk-minutes N       chunk length for long recordings (default 15)
  --no-chunks             transcribe in one pass; no resume if interrupted
  --no-vad                do not skip silence
  --no-prompt             drop the domain vocabulary hint
  --keep-work             keep the intermediate audio after a successful run
  -h, --help              this text

Environment: WHISPER_MODEL, WHISPER_LANG, WHISPER_MODEL_DIR
EOF
  exit 0
}

# ---------------------------------------------------------------- settings --
MODEL="${WHISPER_MODEL:-large-v3}"
LANG="${WHISPER_LANG:-ru}"
MODEL_DIR="${WHISPER_MODEL_DIR:-$HOME/.cache/whisper-cpp/models}"
MODEL_REPO="https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
VAD_MODEL="ggml-silero-v5.1.2.bin"
VAD_REPO="https://huggingface.co/ggml-org/whisper-vad/resolve/main"
PROMPT_FILE="$ROOT/prompts/whisper-initial-prompt.ru.txt"

USE_VAD=1
USE_CHUNKS=1
CHUNK_MINUTES=15
CHUNK_THRESHOLD_SEC=1800   # recordings longer than this are chunked
FORCE=0
LIST_ONLY=0
KEEP_WORK=0
TARGETS=()

THREADS="$(sysctl -n hw.perflevel0.logicalcpu 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"
AUDIO_EXT="m4a mp3 wav aac flac ogg opus aiff aif caf mp4 m4v mov"

# ----------------------------------------------------------------- helpers --
if [ -t 2 ]; then B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=''; D=''; G=''; Y=''; R=''; N=''; fi

info() { printf '%s\n' "${B}$*${N}" >&2; }
note() { printf '%s\n' "${D}$*${N}" >&2; }
warn() { printf '%s\n' "${Y}warning:${N} $*" >&2; }
die()  { printf '%s\n' "${R}error:${N} $*" >&2; exit 1; }

hms() {
  local s=${1%.*}
  printf '%d:%02d:%02d' $((s/3600)) $(((s%3600)/60)) $((s%60))
}

# ------------------------------------------------------------------- args ---
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)      usage ;;
    -f|--force)     FORCE=1 ;;
    -l|--list|--dry-run) LIST_ONLY=1 ;;
    --model)        MODEL="${2:?--model needs a name}"; shift ;;
    --lang)         LANG="${2:?--lang needs a code}"; shift ;;
    --threads)      THREADS="${2:?--threads needs a number}"; shift ;;
    --chunk-minutes) CHUNK_MINUTES="${2:?--chunk-minutes needs a number}"; shift ;;
    --no-chunks)    USE_CHUNKS=0 ;;
    --no-vad)       USE_VAD=0 ;;
    --keep-work)    KEEP_WORK=1 ;;
    --prompt-file)  PROMPT_FILE="${2:?--prompt-file needs a path}"; shift ;;
    --no-prompt)    PROMPT_FILE="" ;;
    -*)             die "unknown option: $1  (try --help)" ;;
    *)              TARGETS+=("$1") ;;
  esac
  shift
done

# --------------------------------------------------------------- toolchain --
command -v whisper-cli >/dev/null 2>&1 || die "whisper-cli not found. Install it with:
    brew install whisper-cpp"
command -v afconvert >/dev/null 2>&1 || die "afconvert not found (it ships with macOS)"

MODEL_PATH="$MODEL_DIR/ggml-$MODEL.bin"
if [ ! -f "$MODEL_PATH" ]; then
  info "Downloading the $MODEL model (one time, a few GB) → $MODEL_PATH"
  mkdir -p "$MODEL_DIR"
  curl -fL --progress-bar -o "$MODEL_PATH.part" "$MODEL_REPO/ggml-$MODEL.bin" \
    || die "could not download ggml-$MODEL.bin — check the model name"
  mv "$MODEL_PATH.part" "$MODEL_PATH"
fi

VAD_ARGS=()
if [ "$USE_VAD" -eq 1 ]; then
  if [ ! -f "$MODEL_DIR/$VAD_MODEL" ]; then
    note "fetching the VAD model ($VAD_MODEL, ~1 MB)"
    curl -fL -s -o "$MODEL_DIR/$VAD_MODEL.part" "$VAD_REPO/$VAD_MODEL" \
      && mv "$MODEL_DIR/$VAD_MODEL.part" "$MODEL_DIR/$VAD_MODEL" \
      || { warn "VAD model download failed; continuing without VAD"; USE_VAD=0; }
  fi
  # split only on a full second of silence, and pad segments, so sentences stay
  # whole — aggressive splitting costs the recogniser its context
  [ "$USE_VAD" -eq 1 ] && VAD_ARGS=(--vad -vm "$MODEL_DIR/$VAD_MODEL" -vsd 1000 -vp 300)
fi

PROMPT_ARGS=()
if [ -n "$PROMPT_FILE" ] && [ -f "$PROMPT_FILE" ]; then
  PROMPT_ARGS=(--prompt "$(tr '\n' ' ' < "$PROMPT_FILE")")
fi

# ------------------------------------------------------- collect the work ---
find_audio() {
  local args=() ext first=1
  for ext in $AUDIO_EXT; do
    if [ $first -eq 1 ]; then args+=(-iname "*.$ext"); first=0
    else args+=(-o -iname "*.$ext"); fi
  done
  find "$1" -type f \( "${args[@]}" \) -not -path '*/.*' 2>/dev/null | LC_ALL=C sort
}

CANDIDATES=()
if [ ${#TARGETS[@]} -eq 0 ]; then
  while IFS= read -r p; do [ -n "$p" ] && CANDIDATES+=("$p"); done < <(find_audio "$ROOT")
else
  for t in "${TARGETS[@]}"; do
    [ -e "$t" ] || t="$ROOT/$t"
    if   [ -d "$t" ]; then while IFS= read -r p; do [ -n "$p" ] && CANDIDATES+=("$p"); done < <(find_audio "$t")
    elif [ -f "$t" ]; then CANDIDATES+=("$t")
    else die "no such file or folder: $t"; fi
  done
fi

[ ${#CANDIDATES[@]} -gt 0 ] || die "no audio found under $ROOT"

TODO=()
for src in "${CANDIDATES[@]}"; do
  base="${src%.*}"
  if [ -s "$base.txt" ] && [ "$FORCE" -eq 0 ]; then
    note "skip (already transcribed): ${src#$ROOT/}"
  else
    TODO+=("$src")
  fi
done

if [ ${#TODO[@]} -eq 0 ]; then
  info "Nothing to do — every recording already has a transcript. Use --force to redo one."
  exit 0
fi

info "${#TODO[@]} recording(s) · model=$MODEL lang=$LANG threads=$THREADS vad=$([ $USE_VAD -eq 1 ] && echo on || echo off)"
for src in "${TODO[@]}"; do note "  • ${src#$ROOT/}"; done
[ "$LIST_ONLY" -eq 1 ] && exit 0

# ------------------------------------------------------------------- work ---
# one whisper pass over one wav; $1 = wav, $2 = output prefix, $3 = log
#
# -mc 0 is not optional. By default whisper feeds what it just decoded back in as
# context for the next window, so one repeated sentence reinforces itself and the
# decoder never escapes — a single loop at 14:18 once ate the following 30 minutes
# of a meeting, emitting "Я не знаю, как это работает" 1339 times. Decoding each
# window without carrying text over costs a little cross-sentence coherence and
# buys a transcript that cannot run away.
run_whisper() {
  whisper-cli -m "$MODEL_PATH" -f "$1" -l "$LANG" -t "$THREADS" \
    -bs 5 -bo 5 -sns -mc 0 -pp -osrt -of "$2" \
    "${VAD_ARGS[@]}" "${PROMPT_ARGS[@]}" >>"$3" 2>&1
}

failed=0
for src in "${TODO[@]}"; do
  base="${src%.*}"
  name="$(basename "$base")"
  rel="${src#$ROOT/}"
  log="$base.transcribe.log"
  work="$(dirname "$src")/.${name}.work"

  dur="$(afinfo "$src" 2>/dev/null | awk -F': ' '/estimated duration/ {print $2}' | awk '{print $1}')"
  dur="${dur:-0}"
  : >"$log"

  printf '\n'
  info "▶ $rel  ($(hms "$dur"))"

  # a folder called 20260909 holding "meeting 8.09.2026.m4a" is usually a typo in
  # one of them — outputs inherit the file's name, so say so before they multiply
  folder_date="$(basename "$(dirname "$src")")"
  file_date="$(printf '%s' "$name" | grep -oE '[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4}' | head -1)"
  if [ -n "$file_date" ] && printf '%s' "$folder_date" | grep -qE '^[0-9]{8}$'; then
    file_ymd="$(printf '%s' "$file_date" | awk -F. '{printf "%04d%02d%02d", $3, $2, $1}')"
    [ "$file_ymd" = "$folder_date" ] || warn "date in file name ($file_date) ≠ folder ($folder_date) — check which one is right"
  fi

  # pre-flight: catch broken inputs before spending GPU time on them.
  # a 0-byte file (Finder leaves one behind, type code 'brok') is an interrupted copy.
  size=$(stat -f %z "$src" 2>/dev/null || echo 0)
  if [ "$size" -eq 0 ]; then
    warn "$rel is empty (0 bytes) — an interrupted copy; re-copy the recording and re-run"
    failed=$((failed+1)); continue
  fi
  if ! awk -v d="$dur" 'BEGIN{exit !(d>1)}'; then
    warn "$rel is unreadable (no duration reported) — damaged file or incomplete copy"
    failed=$((failed+1)); continue
  fi

  chunked=$USE_CHUNKS
  awk -v d="$dur" -v t="$CHUNK_THRESHOLD_SEC" 'BEGIN{exit !(d>t)}' || chunked=0
  started=$(date +%s)

  if [ "$chunked" -eq 0 ]; then
    # ---- short recording: one pass, nothing to resume
    wav="$(mktemp -d "${TMPDIR:-/tmp}/transcribe.XXXXXX")/audio.wav"
    note "  converting audio…"
    if ! afconvert -f WAVE -d LEI16@16000 -c 1 "$src" "$wav" >>"$log" 2>&1; then
      warn "could not decode $rel — see ${log#$ROOT/}"; failed=$((failed+1)); continue
    fi
    note "  transcribing…"
    if ! run_whisper "$wav" "$base" "$log"; then
      warn "whisper-cli failed on $rel — see ${log#$ROOT/}"; failed=$((failed+1)); rm -rf "$(dirname "$wav")"; continue
    fi
    python3 - "$base.srt" "$base.txt" <<'PY' 2>>"$log" || true
import re, sys
src, dst = sys.argv[1], sys.argv[2]
blocks = re.split(r"\n\s*\n", open(src, encoding="utf-8").read().strip())
out = []
for b in blocks:
    lines = [l for l in b.split("\n") if l.strip()]
    if len(lines) >= 2 and "-->" in lines[1 if lines[0].strip().isdigit() else 0]:
        out.append(" ".join(l.strip() for l in lines[(2 if lines[0].strip().isdigit() else 1):]))
open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
    rm -rf "$(dirname "$wav")"
  else
    # ---- long recording: chunk it, so an interrupted run picks up where it left off
    if [ -f "$work/chunks.json" ] && [ "$FORCE" -eq 0 ]; then
      note "  resuming from $(basename "$work")"
    else
      [ "$FORCE" -eq 1 ] && rm -rf "$work"
      mkdir -p "$work"
      note "  converting audio…"
      if ! afconvert -f WAVE -d LEI16@16000 -c 1 "$src" "$work/full.wav" >>"$log" 2>&1; then
        warn "could not decode $rel — see ${log#$ROOT/}"; failed=$((failed+1)); continue
      fi
      note "  splitting into ~${CHUNK_MINUTES}-minute chunks (cut at pauses)…"
      if ! python3 "$ROOT/tools/chunker.py" split "$work/full.wav" "$work" --minutes "$CHUNK_MINUTES" >>"$log" 2>&1; then
        warn "could not split $rel — see ${log#$ROOT/}"; failed=$((failed+1)); continue
      fi
      rm -f "$work/full.wav"
    fi

    total=$(ls "$work"/chunk_*.wav 2>/dev/null | wc -l | tr -d ' ')
    n=0; chunk_failed=0
    for cw in "$work"/chunk_*.wav; do
      n=$((n+1))
      prefix="${cw%.wav}"
      if [ -s "$prefix.srt" ]; then
        note "  chunk $n/$total — done already"
        continue
      fi
      cstart=$(date +%s)
      printf '%s' "${D}  chunk $n/$total — transcribing…${N}" >&2
      if run_whisper "$cw" "$prefix" "$log"; then
        printf '\r%s\n' "${D}  chunk $n/$total — done in $(hms $(( $(date +%s) - cstart )))${N}" >&2
      else
        printf '\r%s\n' "${Y}  chunk $n/$total — FAILED${N}" >&2
        chunk_failed=1; break
      fi
    done
    if [ "$chunk_failed" -eq 1 ]; then
      warn "$rel stopped part-way — re-run to resume (see ${log#$ROOT/})"
      failed=$((failed+1)); continue
    fi

    note "  merging chunks…"
    if ! python3 "$ROOT/tools/chunker.py" merge "$work" --out "$base" >>"$log" 2>&1; then
      warn "could not merge chunks for $rel — see ${log#$ROOT/}"; failed=$((failed+1)); continue
    fi
    [ "$KEEP_WORK" -eq 1 ] || rm -rf "$work"
  fi

  elapsed=$(( $(date +%s) - started ))

  # timestamped paragraphs — the version meant for humans and for summarising
  if [ -s "$base.srt" ]; then
    python3 "$ROOT/tools/srt_to_md.py" "$base.srt" \
        --title "$name" --audio "$(basename "$src")" --out "$base.transcript.md" >>"$log" 2>&1 \
      || warn "could not build $name.transcript.md — see ${log#$ROOT/}"
  fi

  # sanity checks — a qc warning means: listen to that spot before trusting the text
  speech="$(grep -o 'Reduced audio from [0-9]* to [0-9]*' "$log" 2>/dev/null | awk '{s += $6} END {if (s) printf "%.1f", s/16000}')"
  python3 "$ROOT/tools/qc.py" "$base.srt" --duration "$dur" ${speech:+--speech "$speech"} >&2 || true

  words="$(wc -w < "$base.txt" | tr -d ' ')"
  speed="$(awk -v d="$dur" -v e="$elapsed" 'BEGIN{ if (e>0) printf "%.1f", d/e; else printf "?" }')"
  printf '%s\n' "  ${G}✓${N} $words words in $(hms "$elapsed") (${speed}× realtime)" >&2
  note "    ${base#$ROOT/}.txt · .srt · .transcript.md"
done

printf '\n'
if [ "$failed" -gt 0 ]; then
  die "$failed recording(s) failed. Check the .transcribe.log next to them."
fi
info "Done. Next: open Claude Code here and run /meeting-notes for summaries and action points."
