# Barge-in implementation report

Date: 2026-07-14  
Branch: `barge-in`

## Completed

- Added a Swift `AVAudioEngine` helper that enables voice processing on both
  input and output, plays mono PCM or a WAV, emits echo-cancelled mic frames,
  stops the player on a framed control command, reports every probe milestone,
  enforces a 10-second probe deadline, and handles SIGTERM/SIGINT.
- Added audio-engine configuration-change handling. An input/output route
  change stops the active engine and emits `helper_unavailable`, which makes
  the Python service close the helper and use plain VoiceMode for the turn.
- Added the Python `handsfree` MCP server. It fetches the same Kokoro WAV used
  by the existing stack, runs Silero ONNX VAD during playback, requires five
  consecutive voiced frames, closes the helper, and calls VoiceMode 8.11.0
  `converse(skip_tts=True, wait_for_response=True)` for listening.
- Added a half-duplex fallback. Helper or local dependency failures close the
  helper mic before calling plain VoiceMode `converse`; malformed or unknown
  helper frames take the same fallback path.
- The Python launcher starts each helper in its own process group and tears
  down every registered group on normal close, interpreter exit, SIGINT, or
  SIGTERM so a server crash cannot leave the microphone engine running.
- Added install and `talk` integration, the nine-step checklist, and an
  automated verification script.

## Design interpretation

The design says the wrapper should call `converse` over Claude Code’s existing
VoiceMode MCP connection. MCP servers cannot borrow another server connection
owned by the Claude Code client. The wrapper imports VoiceMode’s registered
`converse` function and calls it in-process. This keeps VoiceMode source
untouched and executes the same listen, WebRTC VAD, Whisper, hallucination
guard, and logging code.

## Automated evidence

Recorded after the final verification run. The synthetic latency test feeds a
speech WAV into the live session scheduler while simulated assistant playback
runs; the VAD test double isolates debounce and stop scheduling from acoustic
hardware variability.

```text
Swift release build: PASS (7.87s clean relink)
Python tests: 15 passed in 0.32s
Synthetic speech-WAV stop latency: 147.1ms (PASS, target <200ms)
Python compile: PASS
AEC ownership grep: PASS; no sounddevice or NonBlockingAudioPlayer path
MCP schema smoke test: PASS; speak_and_listen is registered
Kokoro -> Whisper regression: SKIPPED; both local health endpoints were down
```

The exact `swift build -c release` command was attempted first. SwiftPM tried to
start a second `sandbox-exec` inside the executor sandbox and macOS rejected the
nested sandbox. Re-running the same release build with SwiftPM’s own sandbox
disabled passed; the outer workspace sandbox remained active. A normal Terminal
does not need this executor-only flag.

Code inspection found two reasons the old probe could show a blank terminal:
it emitted no progress before every AVFoundation call completed, and its only
stdout write used buffered `print`. The probe also constructed an
`AVAudioPlayerNode` even though it never plays audio. The revised probe removes
that unrelated component lookup, flushes each milestone, waits for a real mic
frame, and runs a watchdog on a separate dispatch queue. A command-line run does
not need a CFRunLoop pump for the audio tap callback; AVAudioEngine delivers it
from the render path after `start()`.

Inside this executor sandbox, the revised probe prints `engine created` and
then CoreAudio raises an Objective-C exception while creating `inputNode`
(`required condition is false: comp != nullptr`). This happens before microphone
permission can be requested. Native playback, the two runtime voice-processing
flags, the ten-second stall watchdog, and clean SIGTERM shutdown remain in the
manual verification gate below.

## Bluetooth route caveat

Bluetooth devices can change the active CoreAudio route when their microphone
profile is selected, including changes to channel count and sample rate. Apple
stops and uninitializes `AVAudioEngine` when its I/O unit sees one of those
hardware-format changes. This version ends the barge-in attempt and falls back
to plain VoiceMode for that turn instead of rebuilding the voice-processing
graph mid-sentence. The next turn starts a fresh helper against the new route.
AirPods and other Bluetooth devices therefore need the dedicated device-switch
check in `plans/barge-in-verify.md` in addition to the built-in-speaker pass.

## Needs Sam

- Run `./install.sh` from a normal Terminal. This executor stayed inside the
  worktree as requested; its network sandbox also blocked the PyPI resolution
  needed for the new Silero/ONNX environment, so it did not register the user
  MCP entry or change launch services.
- The sandbox used for this build denies CoreAudio component lookup before the
  helper reaches the TCC prompt. Run `handsfree-audio-helper --probe` from a
  normal Terminal, approve microphone access, and confirm both voice-processing
  flags are true.
- Run the nine-step checklist in `plans/barge-in-verify.md`. Its acoustic
  checks require a person speaking over the built-in speakers and cannot be
  replaced by the synthetic loopback.
- The first 100–300 ms after an interruption can be clipped by the deliberate
  helper-to-VoiceMode mic handoff.
- This worktree points at Git metadata under
  `/Users/samzoloth/Projects/handsfree/.git`, outside the executor’s writable
  root. Git could not create `index.lock`, so the required conventional commit
  could not be written here. After reviewing the diff, run:

  ```sh
  git add . ':!plans' && git add -f plans/barge-in-verify.md plans/barge-in-report.md
  git commit -m "feat: add echo-cancelled voice barge-in"
  ```
