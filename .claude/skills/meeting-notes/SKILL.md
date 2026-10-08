---
name: meeting-notes
description: Transcribe any new meeting recordings in this folder (local whisper.cpp) and write a <name>.summary.md next to each transcript — TL;DR, decisions, action points with owners and timestamps, open questions. Use when the user asks for meeting notes, a summary, a protocol, "конспект", "протокол", "итоги встречи", or after dropping a new recording into a YYYYMMDD/ folder.
argument-hint: "[folder or file, e.g. 20260813] [--force]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# /meeting-notes — transcripts and notes for every new recording

Work in the folder this skill lives in (the kit root: it holds `transcribe.py`,
`prompts/`, `tools/` and the `YYYYMMDD/` recording folders). All output goes next to
the audio. Nothing leaves the machine except the transcript text you read.

Arguments (`$ARGUMENTS`): optionally a folder (`20260813`) or a single file to limit
the run; `--force` to redo summaries that already exist.

## 1. Transcribe what is new

Run the transcriber. Pick the interpreter that exists: on Windows `python`, on
macOS/Linux `python3`. Pass the folder/file argument through if one was given.

```
python transcribe.py            # Windows
python3 transcribe.py           # macOS / Linux
```

- It skips recordings that already have a `.txt`, so this is cheap when nothing is new.
- It can take a long time (minutes per recording). Run it in the foreground with a
  generous timeout, or in the background and poll the `.transcribe.log`. Do not kill it:
  long recordings resume from the last finished chunk, but a killed short one restarts.
- If it fails with "whisper-cli not found" / "ffmpeg not found", stop and tell the user
  exactly which install step from README.md is missing. Do not try to install things
  yourself without asking.
- Capture every `qc: WARNING` line it prints: they go into the summary (step 4).

## 2. Find transcripts without a summary

List `**/*.transcript.md` (ignore anything under hidden `.*.work/` folders). For each
one, a summary is needed when `<same name>.summary.md` is missing, or `--force` was
given. If there is nothing to do, say so and stop.

## 3. Write the summary — follow `prompts/summary-prompt.md` exactly

Read `prompts/summary-prompt.md` first. It carries the project context (the block
«О проекте»), the rules (never invent; timestamp every decision, action and question;
attribute only what is unambiguous; flag garbled terms) and the exact output
template. Then:

1. Read the transcript end to end. For long ones (over ~40 minutes of audio, i.e. a
   large file), read it in pieces of roughly 30–45 minutes of timestamps and take notes
   per piece, then merge as the prompt describes.
2. Write `<name>.summary.md` next to `<name>.transcript.md`, in the template from the
   prompt, in the language the meeting was held in.
3. Fill the header from facts you have: file name from the folder, duration from the
   first line of the transcript («Transcript · h:mm:ss»), date from the folder name
   `YYYYMMDD` or the file name.
4. Do not add sections the template does not have; do not drop the «Открытые вопросы»
   or «Поручения» sections even if empty (write «нет»).

## 4. Quality flags

If `transcribe.py` printed `qc: WARNING` lines for this recording, or the transcript
contains «[same line repeated N× — likely a recogniser loop]», add a short section at
the top of the summary, right under the header:

```
> ⚠️ Качество расшифровки: <one line per warning, with the timestamp> — проверьте по аудио.
```

## 5. Report

Finish with one short message: which recordings were transcribed, which summaries
were written (paths), and the quality warnings, if any. Remind that the notes were
generated from a machine transcript and not reviewed by a participant.
