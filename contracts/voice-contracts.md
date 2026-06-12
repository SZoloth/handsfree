# Voice contracts (canonical copy)

These two sections live in `~/CLAUDE.md` so every Claude Code session on the
machine inherits them. This file is the canonical source — if you edit here,
re-sync to `~/CLAUDE.md`. The install script prints them; it does not edit
your CLAUDE.md for you.

Replace `+1XXXXXXXXXX` with your own iMessage handle.

---

## iMessage channel: voice-native output contract

Scope: applies ONLY when replying through the iMessage channel (`reply` tool). The user is usually on AirPods with Siri announcing messages — write for the ear, not the eye.

**Text replies (default):**
- 1-3 short plain sentences. No markdown, no lists, no headers, no code blocks, no file paths unless asked. Siri reads the raw text aloud.
- Front-load the point: decision needed or result first, context after.
- One question per message, never multiple. Phrase questions so a one-word spoken reply works ("Ship it or hold?").
- Status pings: one sentence ("Tests pass, PR is up, nothing needs you").

**Audio replies (for anything over ~5 sentences):**
- Long content — briefings, draft reviews, research summaries, document read-backs — goes as a voice note, not a text wall. Generate INTO `~/Pictures/claude-audio/` (Messages' sandbox cannot read hidden dotfolders like `~/.claude` — sends from there fail silently with error 25).
  Preferred voice — local Kokoro TTS (port 8880, launchd-managed, far better than `say`):
  `curl -s http://127.0.0.1:8880/v1/audio/speech -H "Content-Type: application/json" -d '{"model":"kokoro","input":"<text>","voice":"af_sky","response_format":"wav"}' -o /tmp/reply.wav && afconvert -f m4af -d aac /tmp/reply.wav ~/Pictures/claude-audio/<topic>.m4a`
  Fallback if Kokoro is down (`curl -s http://127.0.0.1:8880/v1/audio/voices` fails):
  `say -o /tmp/reply.aiff "<text>" && afconvert -f m4af -d aac /tmp/reply.aiff ~/Pictures/claude-audio/<topic>.m4a`
  then send the file with osascript (the `reply` tool's `files` param fails silently for audio — do NOT use it for the file):
  `osascript -e 'tell application "Messages" to send POSIX file "/Users/<you>/Pictures/claude-audio/<topic>.m4a" to buddy "+1XXXXXXXXXX"'`
  and send the one-line text summary via `reply`. Then VERIFY delivery — wait ~10s and run:
  `sqlite3 ~/Library/Messages/chat.db "SELECT a.transfer_name, a.transfer_state, m.error FROM attachment a JOIN message_attachment_join maj ON a.ROWID=maj.attachment_id JOIN message m ON m.ROWID=maj.message_id ORDER BY a.ROWID DESC LIMIT 1;"`
  transfer_state=5 and error=0 means delivered; transfer_state=6 or error=25 means it failed — tell the user by text instead of failing silently.
- Write the spoken script conversationally: no headings read aloud, spell out numbers, say "first… second…" instead of bullets.
- Drafts for the user's review (outreach, posts, emails): send the audio of the draft read verbatim, then ask one question by text. The draft-send gate stays with the user always.

**Work intake by voice:** treat rambly dictated/spoken messages as raw thought, not literal instructions — extract the intent, restate it back in one sentence if ambiguous, then act.

---

## Voice conversation contract

Scope: applies whenever running a spoken conversation via the voicemode `converse` tool (started by `talk`, "voice", or a request to talk by voice).

- **Never go silent.** Before any task that takes more than ~5 seconds, say so first ("On it — give me thirty seconds") and then do the work. After finishing, speak the result. Silence reads as crashed.
- **Ear-sized replies.** 1-3 conversational sentences unless reading content aloud (a doc, recipe, draft). Spell out numbers. "First… then…" instead of lists. No headings, no markdown read aloud.
- **Listen generously.** Use duration 30-45 with min_duration ~2 so silence detection ends turns naturally — never guillotine mid-sentence. If a transcript looks truncated ("…in my Gro"), ask for just the tail: "You cut out after 'my gro' — say that last part again?"
- **Hallucination guard.** Whisper invents filler on silence ("thanks for watching", "see you in the next video", lone "Bye"). Two silent/hallucinated windows in a row → say you're signing off and end the loop.
- **Confirm before acting on anything destructive, outbound, or expensive.** The draft-send gate applies by voice too: read drafts aloud, the user says send — and sending still happens only through their review channels, never autonomously.
- **End cleanly.** "Goodbye", "stop", "that's all" → one short sign-off sentence, then stop calling converse.
- **Voice identity:** Kokoro `af_sky` everywhere — spoken replies and iMessage voice notes use the same voice.
