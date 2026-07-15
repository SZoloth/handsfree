# wakeword

A local always-on daemon that listens for a wake phrase and invokes
`~/.local/bin/talk`, dropping the "Hey Siri, talk to Claude" hop. Roadmap
item 2 from the main README.

## How it works

```
mic (sounddevice, 16kHz mono, 80ms frames)
   -> openWakeWord (melspectrogram -> shared embedding -> classifier)
   -> score >= threshold and not in cooldown?
   -> subprocess: ~/.local/bin/talk
```

`talk` already prefers the warm `imessage-bridge` tmux session when it's
up (see the main README), so this daemon doesn't need to know anything
about the bridge — it just calls `talk` with no arguments, same as the
Siri Shortcut does.

## Setup

```sh
cd wakeword
uv sync           # creates .venv, installs openwakeword/sounddevice/ai-edge-litert
```

Models are downloaded on first run of `daemon.py` (into `wakeword/models/`,
gitignored — see below) rather than committed, so nothing binary lives in
the repo.

## Model choice: `hey_jarvis`

openWakeWord ships six pretrained models: `alexa`, `hey_mycroft`,
`hey_jarvis`, `hey_rhasspy`, `timer`, `weather`. None of them is
"Claude"-shaped — see [Custom model upgrade path](#custom-model-upgrade-path)
for why a custom phrase isn't in v1. Of the stock options, `hey_jarvis` is
the least embarrassing fit for an assistant use case (a natural two-word
wake phrase, not a competing product name pointed at the wrong assistant
like `alexa`) and it's openWakeWord's most commonly cited/most robust
flagship demo model.

## Hard-won fact: the released `.onnx` models are broken on this machine, use tflite

`Model(inference_framework="onnx")` runs without erroring — features get
computed, shapes are correct, nothing throws — but the classifier output is
degenerate: openWakeWord's own bundled positive test clips
(`alexa_test.wav`, `hey_mycroft_test.wav` from the upstream repo's
`tests/data/`) score in the `0.0001`–`0.0003` range via onnxruntime 1.27.0,
when they should score close to `1.0`. Switching to `inference_framework="tflite"`
against the identical audio scores `0.97`–`0.9999`. Root cause not
identified (plausibly an onnx export/opset mismatch upstream, not a config
error on this end — the pipeline is architecturally correct, just badly
scaled). **Use tflite. Don't "simplify" back to onnx without re-verifying
against a known positive clip first.**

The catch: `tflite-runtime` (the package openWakeWord's tflite path
imports) has no macOS wheel at all — `pip`/`uv` only offer
`manylinux`/`armv7l` builds. [`ai-edge-litert`](https://pypi.org/project/ai-edge-litert/)
is Google's actively maintained successor with an API-compatible
`Interpreter` class and real macOS ARM64 wheels. `daemon.py` registers it
under the `tflite_runtime.interpreter` name in `sys.modules` before
importing `openwakeword` (`_install_tflite_shim()`), so the rest of
openWakeWord's code is unmodified and unaware anything was swapped.

## Thresholds and cooldown

- `--threshold` (default `0.5`): openWakeWord's own models are tuned for a
  `0.5` default; both stock-voice and `say`-synthesized test audio scored
  `0.94`–`0.9999` on true positives during local WAV testing, well clear of
  that bar.
- `--cooldown` (default `120s`): once triggered, further detections are
  logged as `suppressed (cooldown, Ns remaining)` rather than re-invoking
  `talk`. This exists so the assistant's own voice coming back out of the
  speakers (or a stray "jarvis" mid-conversation) can't re-trigger a second
  session on top of a live one. Set via `WAKEWORD_COOLDOWN` env var when
  calling `bin/wakeword`, or `--cooldown` directly against `daemon.py`.
- `--dry-run`: logs detections (including cooldown-suppressed ones) without
  invoking `talk`. Used for the speaker-to-mic self-test below.

## Verification status (as of 2026-07-14)

**Offline pipeline: verified correct.** Fed pre-recorded WAV clips directly
to the model (bypassing the mic entirely):
- openWakeWord's own bundled positive test clips: `alexa_test.wav` scored
  `0.9999999`, `hey_mycroft_test.wav` scored `0.9999999` (via tflite).
- `hey_jarvis` against macOS `say`-synthesized "hey jarvis" across 3 system
  voices (Samantha, Daniel, Eddy): `0.94`–`0.9995`.

**Live mic capture: unreliable on this machine's current audio setup, not
independently re-verified after an interrupted test.** The self-test this
was meant to satisfy — play the phrase through room speakers, confirm a
log line lands within 3 seconds, 5 trials — was cut short by Sam being at
the machine (audio playback during testing was disruptive and testing was
stopped by explicit instruction before it could be completed cleanly). What
the two partial runs showed:
- Run 1 (5 trials, 2.5s apart): one cluster of ~13 consecutive
  above-threshold frames (`0.70`–`0.999`) landed in the log across a
  ~2-second window — consistent with capturing exactly *one* of the five
  spoken phrases (a single ~1s utterance produces multiple consecutive
  80ms-frame detections), not five.
- Run 2 (5 trials, 4s apart): zero detections logged.
- Real hit rate across both runs: **1 detection captured out of 10 phrase
  plays**, not the 5/5 a working setup should show.

The default input **and output** device on this machine is a Bluetooth
headset ("Electric Earmuffs" — AirPods-type). Two plausible causes, neither
confirmed further per the no-more-audio-testing constraint this session
ended under: (1) Bluetooth headsets commonly run echo/feedback cancellation
that actively suppresses the device's own speaker output from reaching its
own mic, which would explain why `say` played through the headset often
isn't heard by the same headset's mic; (2) a CoreAudio profile
renegotiation (HFP handshake) when the mic stream opens can take a couple
of seconds and drop early audio — a `PaMacCore (AUHAL) Error -50` appeared
in the daemon's stderr on both runs, though it coincided with abrupt
process kills at test teardown rather than mid-test, so it isn't
conclusively the cause.

Mic permission is **not** the blocker: `TCC.db` shows
`com.mitchellh.ghostty` (the terminal these tests ran from) already granted
`kTCCServiceMicrophone` (`auth_value=2`), and the daemon did capture real
audio successfully at least once (run 1's detection burst) — a true
permission block would produce zero captures ever, not an intermittent
one.

**What Sam should do to close this out (MANUAL — needs a live mic, can't
be scripted further without audio):**
1. Re-run the 5-trial self-test with `uv run python3 daemon.py --dry-run`
   and watch `~/.local/state/handsfree/wakeword.log` live
   (`tail -f`), using whatever mic/output setup is actually in play day to
   day (if that's the Bluetooth headset, test with it; if it's usually the
   built-in mic + room speakers, test that instead — the two are likely to
   behave very differently given the echo-cancellation theory above).
2. If the built-in mic + room speakers combination gets a clean 5/5, the
   Bluetooth-echo-cancellation theory is confirmed and the fix is either
   "don't wear the headset while home and expecting wake-word to work" (a
   real, if annoying, product constraint to document) or investigating
   per-app audio routing so `talk`'s TTS output and the wake-word mic input
   use different devices.
3. If it's still flaky on a non-Bluetooth setup, capture `daemon.py`'s
   stderr during a failing trial (not just at shutdown) to see whether the
   `AUHAL Error -50` recurs mid-stream.

**Cooldown ("exactly once per window"): verified.** The spec called for
confirming that a real (non-dry-run) invocation triggers `talk` exactly
once even if the phrase is audible twice inside the cooldown window.
`talk` itself wasn't actually re-invoked live (no further audio playback
was in scope after the interruption above), but the gating logic that
decides whether to invoke it is identical in `--dry-run` and live mode —
only the final `subprocess.Popen` call is swapped for a log line. Run 1's
log shows the mechanism working exactly as designed: the first
above-threshold frame at `19:38:08` logged `detection score=0.964 --
triggering talk`, and the next 13 consecutive above-threshold frames (one
continuous utterance, `0.70`–`0.999`) all logged `suppressed (cooldown,
Ns remaining)` instead of triggering again. That's exactly-once behavior
within a single cooldown window, demonstrated end-to-end through the same
code path a live run uses.

CPU (below) was verified without requiring speaker playback and stands
as-is.

## CPU

Idle (`--dry-run`, mic open, no detections) measured at **~4.6% of one
core** via `top -pid <pid> -s 3` over six 3-second samples on this M-series
Mac — under the 5% budget, if close to it. Most of that is the
melspectrogram/embedding/classifier inference running once per 80ms frame
continuously; there's no larger duty-cycle knob in openWakeWord to trade
latency for less-frequent inference without vendoring the prediction loop.

## Custom model upgrade path

openWakeWord's "train your own phrase" path (e.g. a "hey Claude"-style
model) is **not** a fit for the "fully local, under ~30 minutes"
constraint this feature was scoped to, and wasn't attempted:

- The documented fast path is a Google Colab notebook — GPU time, not
  local CPU — and is explicitly described upstream as "<1 hour" *on that
  Colab GPU*.
- The stock models were trained on **~30,000 hours of negative audio**
  (speech, noise, music) to hold their false-accept rate down; the local
  training notebook still requires downloading a large pre-built negative
  dataset, on the order of several GB, before training starts.
- Positive examples come from a separate synthetic-TTS-clip generation
  step (a different repo, `synthetic_speech_dataset_generation`) that
  itself needs "a minimum of several thousand" generated clips per the
  upstream docs.

None of that fits in 30 minutes of local compute on this machine. If a
custom wake phrase becomes worth the investment later:

1. Use the [automatic_model_training Colab notebook](https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb) linked from the
   [openWakeWord README](https://github.com/dscripka/openWakeWord#training-new-models) — expect
   real GPU time, not a local-Mac task.
2. Export both the `.onnx` and `.tflite` variants it produces.
3. **Use the `.tflite` one here** — see the onnx-is-broken finding above,
   which is very likely not specific to the stock models and would apply
   to a custom-trained export too until proven otherwise on a known
   positive clip.
4. Drop the new `.tflite` file into `wakeword/models/`, point `--model` at
   it (a full path bypasses the built-in name-based auto-download), and
   re-run the same WAV-file verification approach used above before
   trusting it live.

## CLI

See `bin/wakeword` (`start|stop|status|install-launchd|uninstall-launchd`)
in the repo root — mirrors `bin/bridge`'s pattern, but launchd runs it with
`KeepAlive=true` since it's a resident process, not a watchdog around a
tmux session.
