# Barge-in verification checklist

Run this from `~/Projects/handsfree-wt-bargein` after `./install.sh`. Steps
marked **MANUAL** need Sam in the room. Keep built-in speakers selected for the
first pass; test AirPods separately because Bluetooth latency changes the audio
route.

```sh
source .venv/bin/activate
```

## Before the acoustic checks

```sh
python -m pytest bargein/tests -q -s
swift build -c release
.build/release/handsfree-audio-helper --probe
```

The probe must print `engine created`, both voice-processing enablement lines,
`engine started`, `mic frame received`, and final JSON with
`voice_processing_input` and `voice_processing_output` set to `true`. The first
run may open macOS System Settings. If prompted, click **Allow** for microphone
access, then rerun the probe. A stalled probe prints its last step and exits
nonzero after 10 seconds. Do not continue with a timeout or a false flag.

## 1. Cut-off test — MANUAL

1. Run `talk` and ask for a multi-sentence answer.
2. While the assistant is mid-sentence, say “wait, stop.”
3. Pass when the speaker stops in about 200 ms. If you can say “one
   Mississippi” before it stops, record a failure.

The automated timing companion uses a synthetic speech WAV and prints the
measured stop latency:

```sh
python -m pytest bargein/tests/test_session.py -q -s -m latency
```

## 2. Handoff test — MANUAL

Immediately continue the sentence you started in step 1. Pass when the words
become the next user turn and the assistant responds to them. Record whether
the first word was clipped; v1 knowingly has a 100–300 ms handoff gap.

## 3. Ten-interrupt repeat — MANUAL

Repeat steps 1 and 2 ten times in a quiet room. Write the result as `clean/10`.
Pass at 8/10 or better. A lower score means the Silero threshold or five-frame
debounce needs room-specific tuning.

## 4. Self-interrupt check — MANUAL

Let the assistant finish six multi-sentence answers without speaking. Pass
only if it completes all six. Treat any self-interruption as an AEC ownership
or convergence failure before changing the VAD threshold.

## 5. Noisy-room repeat — MANUAL

Play music or TV at a normal background level. Repeat one interrupt and two
uninterrupted answers. Record clean interrupts and self-interrupts separately.
This is a characterization step for v1, not an 8/10 release gate.

## 6. Long-session check — MANUAL

Complete about 20 spoken turns. Then inspect:

```sh
log show --last 30m --style compact \
  --predicate 'process == "handsfree-audio-helper" OR eventMessage CONTAINS[c] "CoreAudio"' \
  | rg -i 'busy|permission|error|failed' || true
```

Pass when there are no device-busy or microphone-permission failures. Those
errors mean the helper mic tap did not close before VoiceMode opened its mic.

## 7. Mid-conversation audio-device switch — MANUAL

1. Start a multi-sentence assistant response through the built-in speakers.
2. While it is speaking, switch macOS output to AirPods or another device.
3. Pass when the active helper stops, the tool call completes through plain
   VoiceMode without a protocol error, and no `handsfree-audio-helper` process
   remains after the turn.
4. Start another turn. Pass when the fresh helper uses the newly selected
   route. Switch back to the built-in route and repeat once.

This step characterizes Bluetooth profile changes as well as a simple wired or
built-in device switch. The interrupted turn is expected to lose barge-in and
use the half-duplex fallback.

## 8. iMessage voice-note regression — AUTOMATED

With Kokoro and Whisper running, execute:

```sh
PYTHON="$(pwd)/.venv/bin/python" scripts/verify-bargein
```

The final check synthesizes through Kokoro and transcribes through Whisper. It
prints `OK: Kokoro to Whisper round trip`. The script skips this one check when
either service is down; start the services and rerun before signing off.

## 9. Daemon-down fallback — AUTOMATED + MANUAL

The automated test proves that a helper crash closes its microphone before the
plain VoiceMode call:

```sh
python -m pytest bargein/tests/test_service.py bargein/tests/test_fallback.py -q
```

**MANUAL:** In a fresh Claude Code session, remove or disable the `handsfree`
MCP server, run `talk`, and complete one normal turn. Pass when the conversation
uses VoiceMode’s existing half-duplex behavior and surfaces no barge-in error.
Restore it afterward with:

```sh
claude mcp add --scope user handsfree -- \
  uv run --project "$HOME/Projects/handsfree-wt-bargein" handsfree-bargein
```

## Sign-off record

```text
Date/time:
Output route:
Quiet interrupts: __/10
Self-interrupts: __/6
Noisy-room notes:
20-turn device errors:
Mid-turn device-switch result:
Fallback result:
First-word clipping notes:
```
