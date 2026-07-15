# Barge-in implementation report

Date: 2026-07-14  
Branch: `barge-in`

## Completed

- Added a Swift `AVAudioEngine` helper that enables voice processing on both
  input and output, plays mono PCM or a WAV, emits echo-cancelled mic frames,
  stops the player on a framed control command, and handles SIGTERM/SIGINT.
- Added the Python `handsfree` MCP server. It fetches the same Kokoro WAV used
  by the existing stack, runs Silero ONNX VAD during playback, requires five
  consecutive voiced frames, closes the helper, and calls VoiceMode 8.11.0
  `converse(skip_tts=True, wait_for_response=True)` for listening.
- Added a half-duplex fallback. Helper or local dependency failures close the
  helper mic before calling plain VoiceMode `converse`.
- Added install and `talk` integration, the eight-step checklist, and an
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
Swift release build: PASS (4.83s clean relink)
Python tests: 10 passed in 0.36s
Synthetic speech-WAV stop latency: 145.0ms (PASS, target <200ms)
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

The compiled helper’s `--probe` reached `AVAudioPlayerNode` initialization, then
CoreAudio returned no component inside the executor sandbox (`required condition
is false: comp != nullptr`). This happens before microphone permission can be
requested. Native playback, the two runtime voice-processing flags, and clean
SIGTERM shutdown remain in the manual verification gate below.

## Needs Sam

- Run `./install.sh` from a normal Terminal. This executor stayed inside the
  worktree as requested; its network sandbox also blocked the PyPI resolution
  needed for the new Silero/ONNX environment, so it did not register the user
  MCP entry or change launch services.
- The sandbox used for this build denies CoreAudio component lookup before the
  helper reaches the TCC prompt. Run `handsfree-audio-helper --probe` from a
  normal Terminal, approve microphone access, and confirm both voice-processing
  flags are true.
- Run the eight acoustic checks in `plans/barge-in-verify.md`. They require a
  person speaking over the built-in speakers and cannot be replaced by the
  synthetic loopback.
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
