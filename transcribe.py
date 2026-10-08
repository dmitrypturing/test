#!/usr/bin/env python3
"""
transcribe.py — turn meeting recordings into text transcripts, locally.
Works on Windows, macOS and Linux.

  1. drop a recording into  <this folder>/<YYYYMMDD>/<whatever>.m4a
  2. run  python transcribe.py          (python3 on macOS/Linux)
  3. next to each recording you get:

       <name>.txt             plain text, no timestamps
       <name>.srt             subtitles, for jumping back into the audio
       <name>.transcript.md   timestamped paragraphs — the file to read and summarise
       <name>.transcribe.log  whisper's own output, for when something broke

Long recordings are transcribed in chunks, so an interrupted run resumes where it
stopped instead of starting the hours over. Nothing is uploaded: whisper.cpp runs
on this machine.

Needs: python 3.9+, ffmpeg + ffprobe, whisper-cli (whisper.cpp).
`python transcribe.py --setup` downloads whisper.cpp and the models for you.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
BIN = ROOT / "bin"

MODEL_REPO = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
VAD_REPO = "https://huggingface.co/ggml-org/whisper-vad/resolve/main"
VAD_MODEL = "ggml-silero-v5.1.2.bin"
WHISPER_RELEASES = "https://github.com/ggml-org/whisper.cpp/releases/download"
WHISPER_RELEASES_API = "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=30"
# The "latest" version tag often carries only source archives; prebuilt binaries sit on
# the nightly b#### pre-releases. This tag is a known-good fallback (checked 2026-10-08)
# if the API lookup below is unavailable.
WHISPER_FALLBACK_TAG = "b5454"

CHUNK_THRESHOLD_SEC = 1800          # recordings longer than this are chunked
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".aiff",
             ".aif", ".caf", ".mp4", ".m4v", ".mov", ".webm", ".wma", ".amr"}

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


# ----------------------------------------------------------------- console --
def _enable_colour() -> bool:
    if not sys.stderr.isatty():
        return False
    if IS_WIN:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-12)
            mode = ctypes.c_uint()
            if not k.GetConsoleMode(h, ctypes.byref(mode)):
                return False
            return bool(k.SetConsoleMode(h, mode.value | 0x0004))
        except Exception:
            return False
    return True


for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if _enable_colour():
    B, D, G, Y, R, N = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
else:
    B = D = G = Y = R = N = ""


def info(msg: str) -> None: print(f"{B}{msg}{N}", file=sys.stderr, flush=True)
def note(msg: str) -> None: print(f"{D}{msg}{N}", file=sys.stderr, flush=True)
def warn(msg: str) -> None: print(f"{Y}warning:{N} {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"{R}error:{N} {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


# --------------------------------------------------------------- toolchain --
def find_tool(name: str, env: str | None = None, hint: str = "") -> Path | None:
    """Look for an executable: explicit env var, then bin/ next to this script
    (recursively — a release zip unpacks into a sub-folder), then PATH."""
    if env and os.environ.get(env):
        p = Path(os.environ[env]).expanduser()
        if p.is_file():
            return p
        warn(f"{env}={p} does not exist, ignoring")
    exe = name + (".exe" if IS_WIN else "")
    if BIN.is_dir():
        for p in sorted(BIN.rglob(exe)):
            if p.is_file():
                return p
    found = shutil.which(name)
    return Path(found) if found else None


def install_hint(tool: str) -> str:
    if tool == "whisper-cli":
        if IS_MAC:
            return "brew install whisper-cpp"
        return "python transcribe.py --setup   (downloads whisper.cpp into bin/)"
    if IS_WIN:
        return "winget install Gyan.FFmpeg   (then open a new terminal)"
    if IS_MAC:
        return "brew install ffmpeg"
    return "sudo apt install ffmpeg   (or your distro's equivalent)"


def download(url: str, dest: Path, label: str) -> None:
    """Stream a file to dest via a .part file, with a progress line."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "transcribe.py"})
    try:
        with urllib.request.urlopen(req) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done, last = 0, 0.0
            while True:
                buf = resp.read(1 << 20)
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                if time.time() - last > 0.5 and sys.stderr.isatty():
                    pct = f" {done * 100 // total}%" if total else ""
                    print(f"\r{D}  {label}: {done / 1048576:.0f} MB{pct}{N}   ", end="", file=sys.stderr, flush=True)
                    last = time.time()
        if sys.stderr.isatty():
            print("", file=sys.stderr)
        part.replace(dest)
    except Exception as e:
        part.unlink(missing_ok=True)
        die(f"could not download {url}\n    {e}")


def ensure_model(model_dir: Path, model: str) -> Path:
    path = model_dir / f"ggml-{model}.bin"
    if not path.is_file():
        info(f"Downloading the {model} model (one time, up to a few GB) -> {path}")
        download(f"{MODEL_REPO}/ggml-{model}.bin", path, f"ggml-{model}.bin")
    return path


def ensure_vad(model_dir: Path) -> Path | None:
    path = model_dir / VAD_MODEL
    if not path.is_file():
        note(f"fetching the VAD model ({VAD_MODEL}, ~1 MB)")
        try:
            download(f"{VAD_REPO}/{VAD_MODEL}", path, VAD_MODEL)
        except SystemExit:
            warn("VAD model download failed; continuing without VAD")
            return None
    return path


def newest_tag_with(asset: str) -> str:
    """Newest whisper.cpp release (pre-releases included) that ships `asset`."""
    try:
        req = urllib.request.Request(WHISPER_RELEASES_API,
                                     headers={"User-Agent": "transcribe.py", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            releases = json.load(resp)
        for r in releases:
            if any(a.get("name") == asset for a in r.get("assets", [])):
                return r["tag_name"]
        warn(f"no recent release lists {asset}; using {WHISPER_FALLBACK_TAG}")
    except Exception as e:
        note(f"  release lookup failed ({e}); using {WHISPER_FALLBACK_TAG}")
    return WHISPER_FALLBACK_TAG


def setup(cuda: bool, model_dir: Path, model: str, want_vad: bool) -> int:
    """Download whisper.cpp (Windows/Linux) and the models. Idempotent."""
    info("Checking the toolchain")
    for t in ("ffmpeg", "ffprobe"):
        p = find_tool(t)
        print(f"  {t:12s} {G + 'ok' + N if p else R + 'missing' + N}  {p or install_hint('ffmpeg')}", file=sys.stderr)

    cli = find_tool("whisper-cli", "WHISPER_CLI")
    if cli:
        print(f"  {'whisper-cli':12s} {G}ok{N}  {cli}", file=sys.stderr)
    elif IS_MAC:
        print(f"  {'whisper-cli':12s} {R}missing{N}  run: brew install whisper-cpp", file=sys.stderr)
    else:
        arch = platform.machine().lower()
        if IS_WIN:
            if arch in ("amd64", "x86_64"):
                asset = "whisper-bin-win-cuda-12.4.0-x64.zip" if cuda else "whisper-bin-x64.zip"
            elif arch in ("arm64", "aarch64"):
                asset = "whisper-bin-win-cpu-arm64.zip"
            else:
                die(f"no prebuilt whisper.cpp for Windows/{arch}; build it from source")
        else:
            asset = "whisper-bin-ubuntu-x64.tar.gz" if arch in ("amd64", "x86_64") else "whisper-bin-ubuntu-arm64.tar.gz"
        tag = newest_tag_with(asset)
        dest = BIN / asset
        info(f"Downloading whisper.cpp {tag} ({asset}) -> {dest}")
        download(f"{WHISPER_RELEASES}/{tag}/{asset}", dest, asset)
        target = BIN / "whisper"
        target.mkdir(parents=True, exist_ok=True)
        if asset.endswith(".zip"):
            with zipfile.ZipFile(dest) as z:
                z.extractall(target)
        else:
            with tarfile.open(dest) as t:
                if hasattr(tarfile, "data_filter"):
                    t.extractall(target, filter="data")
                else:
                    t.extractall(target)
            for p in target.rglob("whisper-cli"):
                p.chmod(p.stat().st_mode | 0o111)
        dest.unlink()
        cli = find_tool("whisper-cli", "WHISPER_CLI")
        if not cli:
            die(f"unpacked {asset} but found no whisper-cli inside {target}")
        print(f"  {'whisper-cli':12s} {G}ok{N}  {cli}", file=sys.stderr)

    info("Models")
    ensure_model(model_dir, model)
    if want_vad:
        ensure_vad(model_dir)
    print(f"  model dir    {model_dir}", file=sys.stderr)
    info("Setup done. Next: put a recording into <YYYYMMDD>/ and run: python transcribe.py")
    return 0


# ------------------------------------------------------------------- audio --
def duration_of(ffprobe: Path, src: Path) -> float:
    try:
        out = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
            capture_output=True, text=True, timeout=120).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def to_wav(ffmpeg: Path, src: Path, wav: Path, log) -> bool:
    r = subprocess.run([str(ffmpeg), "-y", "-v", "error", "-i", str(src),
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                       stdout=log, stderr=subprocess.STDOUT)
    return r.returncode == 0 and wav.is_file() and wav.stat().st_size > 44


def srt_to_txt(srt: Path, txt: Path) -> None:
    blocks = re.split(r"\n\s*\n", srt.read_text(encoding="utf-8", errors="replace").strip())
    out = []
    for b in blocks:
        lines = [l for l in b.split("\n") if l.strip()]
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if len(lines) >= 2 and "-->" in lines[0]:
            out.append(" ".join(l.strip() for l in lines[1:]))
    txt.write_text("\n".join(out) + "\n", encoding="utf-8")


def speech_seconds(log: Path) -> float | None:
    """Seconds of audio the VAD kept — whisper logs 'Reduced audio from N to M'."""
    kept = [int(m) for m in re.findall(r"Reduced audio from \d+ to (\d+)", log.read_text(encoding="utf-8", errors="replace"))]
    return sum(kept) / 16000 if kept else None


# -------------------------------------------------------------------- main --
def default_threads() -> int:
    if IS_MAC:
        for key in ("hw.perflevel0.logicalcpu", "hw.ncpu"):
            try:
                return int(subprocess.run(["sysctl", "-n", key], capture_output=True, text=True).stdout.strip())
            except Exception:
                pass
    return max(1, (os.cpu_count() or 4))


def find_audio(root: Path) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
            continue
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return sorted(out, key=lambda p: str(p))


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="transcribe.py", add_help=False,
        description="meeting recordings to text, locally",
        epilog="Environment: WHISPER_MODEL, WHISPER_LANG, WHISPER_MODEL_DIR, WHISPER_CLI")
    ap.add_argument("targets", nargs="*", help="folders or files; default: every recording that lacks a transcript")
    ap.add_argument("-h", "--help", action="help", help="this text")
    ap.add_argument("-l", "--list", "--dry-run", dest="list_only", action="store_true", help="show what would run, do nothing")
    ap.add_argument("-f", "--force", action="store_true", help="redo transcripts that already exist")
    ap.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "large-v3"),
                    help="large-v3 (default) | large-v3-turbo | large-v3-turbo-q5_0 | medium | small")
    ap.add_argument("--lang", default=os.environ.get("WHISPER_LANG", "ru"), help="ru (default), en, ... or 'auto'")
    ap.add_argument("--threads", type=int, default=default_threads(), help="decoding threads (default: all cores)")
    ap.add_argument("--chunk-minutes", type=float, default=15, help="chunk length for long recordings (default 15)")
    ap.add_argument("--no-chunks", action="store_true", help="transcribe in one pass; no resume if interrupted")
    ap.add_argument("--no-vad", action="store_true", help="do not skip silence")
    ap.add_argument("--prompt-file", default=str(ROOT / "prompts" / "whisper-initial-prompt.ru.txt"))
    ap.add_argument("--no-prompt", action="store_true", help="drop the domain vocabulary hint")
    ap.add_argument("--keep-work", action="store_true", help="keep the intermediate audio after a successful run")
    ap.add_argument("--whisper-cli", default=None, help="path to whisper-cli(.exe)")
    ap.add_argument("--model-dir", default=os.environ.get("WHISPER_MODEL_DIR") or str(Path.home() / ".cache" / "whisper-cpp" / "models"))
    ap.add_argument("--setup", action="store_true", help="download whisper.cpp (CPU build) and the models, then exit")
    ap.add_argument("--setup-cuda", action="store_true", help="same, but the NVIDIA CUDA build of whisper.cpp (Windows x64)")
    args = ap.parse_args()

    model_dir = Path(args.model_dir).expanduser()
    if args.setup or args.setup_cuda:
        return setup(args.setup_cuda, model_dir, args.model, not args.no_vad)

    if args.whisper_cli:
        os.environ["WHISPER_CLI"] = args.whisper_cli
    whisper = find_tool("whisper-cli", "WHISPER_CLI") or die(f"whisper-cli not found. Install it with:\n    {install_hint('whisper-cli')}")
    ffmpeg = find_tool("ffmpeg") or die(f"ffmpeg not found. Install it with:\n    {install_hint('ffmpeg')}")
    ffprobe = find_tool("ffprobe") or die("ffprobe not found (it ships with ffmpeg)")
    py = sys.executable

    model_path = ensure_model(model_dir, args.model)
    vad_args: list[str] = []
    if not args.no_vad:
        vad = ensure_vad(model_dir)
        if vad:
            # split only on a full second of silence, and pad segments, so sentences
            # stay whole — aggressive splitting costs the recogniser its context
            vad_args = ["--vad", "-vm", str(vad), "-vsd", "1000", "-vp", "300"]

    prompt_args: list[str] = []
    pf = Path(args.prompt_file)
    if not args.no_prompt and pf.is_file():
        text = " ".join(pf.read_text(encoding="utf-8").split())
        if text:
            prompt_args = ["--prompt", text]

    # ----------------------------------------------------- collect the work --
    candidates: list[Path] = []
    if not args.targets:
        candidates = find_audio(ROOT)
    else:
        for t in args.targets:
            p = Path(t)
            if not p.exists():
                p = ROOT / t
            if p.is_dir():
                candidates += find_audio(p)
            elif p.is_file():
                candidates.append(p.resolve())
            else:
                die(f"no such file or folder: {t}")
    if not candidates:
        die(f"no audio found under {ROOT}")

    todo = []
    for src in candidates:
        base = src.with_name(src.stem)
        txt = Path(f"{base}.txt")
        if txt.is_file() and txt.stat().st_size > 0 and not args.force:
            note(f"skip (already transcribed): {rel(src)}")
        else:
            todo.append(src)
    if not todo:
        info("Nothing to do — every recording already has a transcript. Use --force to redo one.")
        return 0

    info(f"{len(todo)} recording(s) · model={args.model} lang={args.lang} threads={args.threads} vad={'on' if vad_args else 'off'}")
    for src in todo:
        note(f"  • {rel(src)}")
    if args.list_only:
        return 0

    # -mc 0 is not optional. By default whisper feeds what it just decoded back in as
    # context for the next window, so one repeated sentence reinforces itself and the
    # decoder never escapes — a single loop once ate the following 30 minutes of a
    # meeting, emitting the same sentence 1339 times. Decoding each window without
    # carrying text over costs a little cross-sentence coherence and buys a
    # transcript that cannot run away.
    def run_whisper(wav: Path, prefix: Path, log) -> bool:
        cmd = [str(whisper), "-m", str(model_path), "-f", str(wav), "-l", args.lang,
               "-t", str(args.threads), "-bs", "5", "-bo", "5", "-sns", "-mc", "0", "-pp",
               "-osrt", "-of", str(prefix)] + vad_args + prompt_args
        r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        return r.returncode == 0 and Path(f"{prefix}.srt").is_file()

    failed = 0
    for src in todo:
        base = src.with_name(src.stem)
        name = src.stem
        log_path = Path(f"{base}.transcribe.log")
        work = src.parent / f".{name}.work"
        dur = duration_of(ffprobe, src)
        log_path.write_text("", encoding="utf-8")

        print("", file=sys.stderr)
        info(f"▶ {rel(src)}  ({hms(dur)})")

        # a folder called 20260909 holding "meeting 8.09.2026.m4a" is usually a typo in
        # one of them — outputs inherit the file's name, so say so before they multiply
        folder = src.parent.name
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", name)
        if m and re.fullmatch(r"\d{8}", folder):
            file_ymd = f"{int(m.group(3)):04d}{int(m.group(2)):02d}{int(m.group(1)):02d}"
            if file_ymd != folder:
                warn(f"date in file name ({m.group(0)}) ≠ folder ({folder}) — check which one is right")

        # pre-flight: catch broken inputs before spending CPU/GPU time on them
        if src.stat().st_size == 0:
            warn(f"{rel(src)} is empty (0 bytes) — an interrupted copy; re-copy the recording and re-run")
            failed += 1
            continue
        if dur <= 1:
            warn(f"{rel(src)} is unreadable (no duration reported) — damaged file or incomplete copy")
            failed += 1
            continue

        chunked = (not args.no_chunks) and dur > CHUNK_THRESHOLD_SEC
        started = time.time()

        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            if not chunked:
                # ---- short recording: one pass, nothing to resume
                tmp = Path(tempfile.mkdtemp(prefix="transcribe."))
                wav = tmp / "audio.wav"
                note("  converting audio…")
                if not to_wav(ffmpeg, src, wav, log):
                    warn(f"could not decode {rel(src)} — see {rel(log_path)}")
                    shutil.rmtree(tmp, ignore_errors=True)
                    failed += 1
                    continue
                note("  transcribing…")
                ok = run_whisper(wav, base, log)
                shutil.rmtree(tmp, ignore_errors=True)
                if not ok:
                    warn(f"whisper-cli failed on {rel(src)} — see {rel(log_path)}")
                    failed += 1
                    continue
                srt_to_txt(Path(f"{base}.srt"), Path(f"{base}.txt"))
            else:
                # ---- long recording: chunk it, so an interrupted run picks up where it left off
                if (work / "chunks.json").is_file() and not args.force:
                    note(f"  resuming from {work.name}")
                else:
                    if args.force:
                        shutil.rmtree(work, ignore_errors=True)
                    work.mkdir(parents=True, exist_ok=True)
                    note("  converting audio…")
                    full = work / "full.wav"
                    if not to_wav(ffmpeg, src, full, log):
                        warn(f"could not decode {rel(src)} — see {rel(log_path)}")
                        failed += 1
                        continue
                    note(f"  splitting into ~{args.chunk_minutes:g}-minute chunks (cut at pauses)…")
                    r = subprocess.run([py, "-I", str(TOOLS / "chunker.py"), "split", str(full), str(work),
                                        "--minutes", str(args.chunk_minutes)], stdout=log, stderr=subprocess.STDOUT)
                    if r.returncode != 0:
                        warn(f"could not split {rel(src)} — see {rel(log_path)}")
                        failed += 1
                        continue
                    full.unlink(missing_ok=True)

                chunks = sorted(work.glob("chunk_*.wav"))
                total = len(chunks)
                chunk_failed = False
                for n, cw in enumerate(chunks, 1):
                    prefix = cw.with_suffix("")
                    srt = Path(f"{prefix}.srt")
                    if srt.is_file() and srt.stat().st_size > 0:
                        note(f"  chunk {n}/{total} — done already")
                        continue
                    cstart = time.time()
                    print(f"{D}  chunk {n}/{total} — transcribing…{N}", end="", file=sys.stderr, flush=True)
                    if run_whisper(cw, prefix, log):
                        print(f"\r{D}  chunk {n}/{total} — done in {hms(time.time() - cstart)}{N}      ", file=sys.stderr, flush=True)
                    else:
                        print(f"\r{Y}  chunk {n}/{total} — FAILED{N}            ", file=sys.stderr, flush=True)
                        chunk_failed = True
                        break
                if chunk_failed:
                    warn(f"{rel(src)} stopped part-way — re-run to resume (see {rel(log_path)})")
                    failed += 1
                    continue

                note("  merging chunks…")
                r = subprocess.run([py, "-I", str(TOOLS / "chunker.py"), "merge", str(work), "--out", str(base)],
                                   stdout=log, stderr=subprocess.STDOUT)
                if r.returncode != 0:
                    warn(f"could not merge chunks for {rel(src)} — see {rel(log_path)}")
                    failed += 1
                    continue
                if not args.keep_work:
                    shutil.rmtree(work, ignore_errors=True)

            elapsed = time.time() - started

            # timestamped paragraphs — the version meant for humans and for summarising
            srt = Path(f"{base}.srt")
            if srt.is_file() and srt.stat().st_size > 0:
                r = subprocess.run([py, "-I", str(TOOLS / "srt_to_md.py"), str(srt), "--title", name,
                                    "--audio", src.name, "--out", f"{base}.transcript.md"],
                                   stdout=log, stderr=subprocess.STDOUT)
                if r.returncode != 0:
                    warn(f"could not build {name}.transcript.md — see {rel(log_path)}")

        # sanity checks — a qc warning means: listen to that spot before trusting the text
        speech = speech_seconds(log_path)
        qc = [py, "-I", str(TOOLS / "qc.py"), f"{base}.srt", "--duration", str(dur)]
        if speech:
            qc += ["--speech", f"{speech:.1f}"]
        subprocess.run(qc, stdout=sys.stderr)

        words = len(Path(f"{base}.txt").read_text(encoding="utf-8", errors="replace").split())
        speed = f"{dur / elapsed:.1f}" if elapsed > 0 else "?"
        print(f"  {G}✓{N} {words} words in {hms(elapsed)} ({speed}× realtime)", file=sys.stderr)
        note(f"    {rel(base)}.txt · .srt · .transcript.md")

    print("", file=sys.stderr)
    if failed:
        die(f"{failed} recording(s) failed. Check the .transcribe.log next to them.")
    info("Done. Next: open Claude Code in this folder and run /meeting-notes for summaries and action points.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(f"\n{Y}interrupted{N} — finished chunks are kept; re-run to resume", file=sys.stderr)
        raise SystemExit(130)
