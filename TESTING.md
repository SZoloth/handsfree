# Manual acoustic test session (~15 minutes, one sitting)

Everything below needs a human with a mouth, speakers, and a microphone —
no agent can run these. Automated coverage (builds, unit tests, guard logic,
the AEC hardware probe) is already green; this session validates the
acoustics. Use the **built-in mic and room speakers** — not a Bluetooth
headset — for the first pass. Bluetooth is a separate later pass.

## Part 1 — Wake word (5 min)

Full details: `wakeword/README.md` (top section). Short form:

1. `cd wakeword && uv sync` (first run downloads the model).
2. `uv run python3 daemon.py --dry-run` — approve the mic prompt if asked.
3. Say "hey jarvis" clearly, 5 times, a few seconds apart, normal room volume.
4. **Pass bar: 5/5 detections in the log within ~1s each.** The earlier 1/10
   result was recorded through a Bluetooth headset whose own echo
   cancellation likely ate the room audio — if you still miss with the
   built-in mic, the threshold needs tuning, not the hardware theory.
5. Only on a clean pass: `wakeword install-launchd` to enable it for real.
   (`wakeword stop` now durably stops it across logins; `uninstall-launchd`
   removes it entirely.)

## Part 2 — Barge-in (10 min)

Full protocol: `plans/barge-in-verify.md`. The must-run subset:

1. Probe (already passed once on this machine 2026-07-14 —
   `voice_processing_input: true, voice_processing_output: true`):
   `./.build/release/handsfree-audio-helper --probe`
2. Register the sidecar per `plans/barge-in-report.md` (install step), start
   a voice session (`talk`), and while the assistant is mid-sentence,
   **talk over it**. Pass bar: it shuts up within "one Mississippi" (~200ms
   design target) and treats what you said as the next turn.
3. Repeat the interrupt 10 times in a quiet room. Pass bar: 8/10 clean.
4. Let it give 6 full answers WITHOUT interrupting. Pass bar: zero
   self-triggers (this is the echo-cancellation convergence check).
5. Mid-conversation, connect or disconnect AirPods. Pass bar: no crash —
   it should degrade to the normal (non-interruptible) voice loop.
6. Regression: text yourself for a voice note; confirm iMessage audio still
   delivers (services must be up: `curl -s localhost:8880/v1/audio/voices`).

## Recording results

Jot pass/fail per step in this file or tell the assistant — failures route
back into fix rounds; a clean sheet closes the roadmap's two hardest items.
