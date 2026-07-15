# handsfree

Voice-native control of a Mac through Claude Code. Talk to your machine, it
talks back, and the same assistant is reachable by iMessage when you walk away.
Everything runs locally — Whisper ears, Kokoro voice, no audio leaves the box.

Built in one evening session (2026-06-11) as part of a screen-minimal work
system; the design notes live in the companion doc
`SamOS/areas/health/screen-minimal-system.md`.

## The product principle

**One assistant, always warm, never silent.**

- *One assistant*: the voice loop and the iMessage channel share a single
  persistent Claude session (the "bridge"). What you said at your desk this
  morning is known to the agent you text from a walk this afternoon.
- *Always warm*: `talk` injects into the running bridge instead of cold-booting
  — measured wake of ~0.6s to dispatch, speech in ~3-5s, versus ~20s cold.
- *Never silent*: behavior contracts require a spoken acknowledgment before any
  long task, ear-sized replies, and audible state cues. Silence reads as
  crashed; the contracts make it impossible by construction.

## Architecture

```
                    you (voice)                you (iMessage, anywhere)
                        │                              │
        "Hey Siri, talk to Claude"                     │
         or `talk` in any terminal                     │
                        │                              │
                        ▼                              ▼
                ┌─────────────────────────────────────────────┐
                │   bridge: persistent claude session (tmux)  │
                │   --channels imessage · voicemode MCP       │
                └──────┬──────────────────────┬───────────────┘
                       │                      │
              converse tool             reply tool / osascript
                       │                      │
            ┌──────────┴─────────┐    voice notes (Kokoro m4a)
            ▼                    ▼            │
   Whisper large-v3-turbo   Kokoro af_sky     ▼
   STT :2022 (local)        TTS :8880     Messages.app → iPhone
                                          (Siri announces on AirPods)
```

Both services are launchd-managed and start at login.

## Install

```sh
./install.sh
```

Idempotent. It installs/starts the services, upgrades Whisper to
large-v3-turbo, registers the VoiceMode MCP at user scope, links `talk` and
`bridge` onto your PATH, and imports the Siri shortcut. It ends by printing
the manual steps (CLAUDE.md contracts, iMessage plugin, two permission
prompts, one iPhone setting).

## Use

| You do | What happens |
| --- | --- |
| `talk` | Voice conversation, instantly if the bridge is up |
| `talk "find my recipe"` | Voice conversation seeded with a task |
| "Hey Siri, talk to Claude" | Same, no terminal — works mid-cooking |
| Text yourself from iPhone | Bridge acts and replies in the thread; long answers arrive as voice notes |
| `bridge start/stop/status` | Manage the persistent session |
| `bridge install-launchd` | One-time: bridge survives reboot (watchdog checks every 5 min) |
| Say "goodbye" / "stop" | Voice loop ends cleanly |
| "Hey Jarvis" (mic always listening) | Local wake word triggers `talk`, no Siri hop — see `wakeword/README.md` |
| `wakeword start/stop/status` | Manage the wake-word daemon |
| `wakeword install-launchd` | One-time: wake-word daemon survives reboot (KeepAlive) |

## Behavior contracts

`contracts/voice-contracts.md` is the canonical copy of the two CLAUDE.md
sections that make this feel like a product instead of a demo: ear-sized
replies, spoken acks before long work, generous listen windows with
truncation recovery, a Whisper-hallucination guard, delivery verification on
every voice note, and a hard human gate on anything outbound.

## Hard-won facts (each cost a debugging session)

1. **Messages' sandbox can't read hidden dotfolders.** Audio sent from
   `~/.claude/...` fails silently with `error=25` while the local chat.db
   happily records the attempt. Generate into `~/Pictures/` and verify
   delivery with the chat.db query in the contract.
2. **The iMessage plugin's `files` param drops audio silently.** Send media
   via `osascript ... send POSIX file`, keep `reply` for text.
3. **Claude Code's REPL prompt is `❯` + a non-breaking space** (` `).
   A plain-space regex never matches it. The empty-prompt guard in `bin/talk`
   matches "❯ + non-alphanumerics" instead.
4. **tmux send-keys text triggers paste detection** — a same-call `Enter` gets
   swallowed and the command sits as a draft. Send text, wait ~0.4s, then send
   `Enter` separately.
5. **Whisper `base` (the default install) is not usable** for conversation:
   it truncates tails and hallucinates YouTube outros from silence.
   large-v3-turbo fixed both, still sub-second locally.
6. **Kokoro's memory-leak mitigation crash-loops under unrelated traffic.**
   VoiceMode sets `UVICORN_LIMIT_MAX_REQUESTS=25` by default (a real fix for
   a real kokoro-fastapi leak — see hexgrad/kokoro#152) so the worker
   recycles periodically. But *anything* hitting the port counts toward
   that cap, including 404s — something on this machine polls a
   nonexistent `/api/health` path (the real route is `/health`) at high
   frequency, exhausting the 25-request budget in 1-2 minutes. launchd's
   `KeepAlive=true` then treats every one of those clean, by-design uvicorn
   shutdowns as a crash and restarts immediately: 1,058 restarts in 31 days
   serving zero real synthesis. Fix: `VOICEMODE_KOKORO_MAX_REQUESTS=2000` in
   `~/.voicemode/voicemode.env` (also set by `install.sh`), regenerated into
   the launchd plist via `voicemode service enable kokoro` — never hand-edit
   the installed plist directly, voicemode owns and regenerates it from this
   template. The `/api/health` poller's source is still unidentified (not
   voicemode or kokoro-fastapi — both only ever call `/health`).
7. **A "suggested next prompt" looks identical to a real draft.** Claude
   Code renders a dim/grey suggested-prompt ghost in the input box after
   most turns (the `--prompt-suggestions` feature). It's not in the input
   buffer — typing overwrites it instantly — but a plain-text scan of the
   pane can't tell it apart from a real draft, so `bin/talk`'s "never
   clobber a draft" fast-path guard saw it as non-empty almost every time
   and silently fell back to the slow cold-boot path. Fixed by capturing
   the pane with ANSI codes preserved (`tmux capture-pane -e`) and treating
   text wrapped in the dim SGR span (`\x1b[2m`) as empty.

## Roadmap

1. **Barge-in** — interrupt the assistant mid-speech (needs a custom mic loop;
   VoiceMode doesn't support it upstream). The biggest remaining gap between
   "demo" and "human."
2. ~~Local wake word ("Claude?") via openWakeWord → `talk`, dropping the
   Siri hop.~~ Built 2026-07-14 with `hey jarvis` (`wakeword start` /
   `wakeword install-launchd`, see `wakeword/README.md`) — the offline
   detection pipeline is verified correct (0.94–0.9999 on true positives),
   the trigger path is bounded (won't spawn a cold session if the bridge
   is down) and self-trigger-guarded (won't stack a second `talk` on an
   ongoing conversation, cooldown timer or not), and `wakeword stop`
   actually stops it under launchd. Still open: live speaker-to-mic
   verification, MANUAL — run the 5-minute checklist at the top of
   `wakeword/README.md` (built-in mic + room speakers, not Bluetooth)
   before `wakeword install-launchd`.
3. ~~launchd auto-start for the bridge so the warm brain survives reboots.~~
   Done 2026-07-14: `bridge install-launchd` (see Use table below).
4. **Streaming TTS** so long reads begin speaking on the first sentence.
