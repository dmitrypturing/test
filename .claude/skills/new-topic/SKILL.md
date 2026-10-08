---
name: new-topic
description: Adapt this meeting-notes kit to a new team or subject — interview the user about the project, participants and vocabulary, then fill in prompts/summary-prompt.md («О проекте») and prompts/whisper-initial-prompt.ru.txt. Use when setting the kit up for the first time, when "новая тема", "настроить под проект", "словарь терминов" is mentioned, or when transcripts keep misspelling the same terms.
allowed-tools: Read, Write, Edit, Glob, Grep
---

# /new-topic — tune the kit for a new subject

Two files make the kit specific to a team. This skill fills both from a short
conversation. Never invent facts about the user's project — ask.

## 1. Ask (one message, 4–6 questions, in the user's language)

- What is the team / project / subject of these meetings? Two or three sentences.
- Who takes part regularly (first names as they are said aloud, and roles)?
- Which terms, product names, abbreviations, system names, client names, place names
  come up often? Ask for 20–60 items; English terms said in Russian speech count.
- Meeting language (default Russian) and whether English terms are mixed in.
- Is there anything the notes should always track (budget figures, deadlines,
  regulatory items, client commitments)?
- Which document types usually need updating after a meeting (plan, contract, deck,
  wiki) — for the «Что сделать по итогам в документах» section.

If the user already has a glossary, a project README or earlier notes, offer to read
them instead of asking.

## 2. Write `prompts/whisper-initial-prompt.ru.txt`

Plain text, **no comments, no headings**: the whole file is handed to the speech
recogniser as a hint. One sentence of context, then a comma-separated term list,
including participants' names. Keep the whole file under ~150 words — whisper uses
only about 200 tokens of it, the rest is ignored. See
`prompts/whisper-initial-prompt.example.txt` for the shape. Spell terms the way they
should appear in the transcript (Cyrillic or Latin as the team writes them).

## 3. Edit `prompts/summary-prompt.md`

Replace only the text under the heading `## О проекте` (between the HTML comment and
the `---`) with 2–6 lines: team and subject, regular participants with roles, the
things the notes must always capture, and the document types for the follow-ups
section. Leave the rules and the template untouched.

## 4. Optionally `CLAUDE.md`

If the user wants, add one or two lines about the project under «О проекте» in
`CLAUDE.md` so every Claude session in this folder knows the context.

## 5. Report

Show both files' new content in the reply and remind: recordings already transcribed
keep their old transcript; re-run `python transcribe.py --force <folder>` to apply the
new vocabulary to one of them.
