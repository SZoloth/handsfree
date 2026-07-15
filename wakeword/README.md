# wakeword

A local always-on daemon that listens for a wake phrase and invokes
`~/.local/bin/talk`, dropping the "Hey Siri, talk to Claude" hop. Roadmap
item 2 from the main README.

**This daemon is opt-in only.** Nothing in this repo auto-installs or
auto-starts it — `install.sh` doesn't reference `wakeword/` at all, and
no launchd job exists until you explicitly run `wakeword install-launchd`
(verified clean as of this writing: no `wakeword` processes, no
`com.handsfree.wakeword` launchd job loaded). Don't install the launchd
job until you've run the manual checklist below at least once.

## Manual verification checklist (~5 minutes, do this before installing)

The automated offline tests below prove the detection *model* works. They
cannot prove the *live mic path* works on your actual audio setup — that
needs a human, a real mic, and real speakers. Do this once before trusting
`wakeword install-launchd`:

1. **Switch to the built-in mic and room speakers, not a Bluetooth
   headset.** System Settings → Sound, or the menu-bar sound icon: set
   input to "MacBook Air Microphone" (or your Mac's built-in mic) and
   output to the built-in speakers. Bluetooth headsets are suspected of
   actively cancelling their own speaker output from their own mic (see
   "Verification status" below) — testing on one will likely produce a
   false failure.
2. **Run dry-run mode**, from `wakeword/`:
   ```sh
   uv run python3 daemon.py --dry-run
   ```
   Wait for `INFO listening` in the output.
3. **In a second terminal, tail the log:**
   ```sh
   tail -f ~/.local/state/handsfree/wakeword.log
   ```
4. **Say "hey jarvis" out loud, clearly, 5 times, a few seconds apart.**
   (You don't need `say`/speakers for this test — your own voice is the
   real-world case that matters.)
5. **What a pass looks like:** 5 separate `detection score=...` or
   `suppressed (cooldown...)` log lines, one per utterance, each within
   ~1-2 seconds of speaking. What a fail looks like: silence in the log
   for some or all of the 5 attempts (this is what happened in the one
   partial test run so far — see "Verification status").
6. Stop the dry-run with Ctrl-C.

**Only after a clean 5/5 pass, run `wakeword install-launchd`** to make it
resident. If it's not 5/5, don't install the launchd job yet — see
"Verification status" below for what's known so far and what to try next.

## How it works

```
mic (sounddevice, 16kHz mono, 80ms frames)
   -> conversation presumed active? (trigger lock, or bridge pane busy)
      -> yes: discard frame, don't even score it
      -> no: score with openWakeWord
   -> score >= threshold and not in cooldown?
   -> bridge session exists?
      -> yes: subprocess ~/.local/bin/talk, write trigger lock
      -> no: macOS notification, log, do NOT spawn anything
```

`talk` already prefers the warm `imessage-bridge` tmux session when it's
up (see the main README). This daemon *does* need to know about the
bridge now — see "Bounded trigger path and self-trigger guard" below for
why a naive "just call `talk`" design was a privacy/spawn-safety bug.

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

## Bounded trigger path and self-trigger guard

Review round 1 flagged two blockers in the original design ("score above
threshold and not in cooldown -> call `talk`, unconditionally"):

1. **Unbounded spawn on a false positive.** If the warm `imessage-bridge`
   tmux session was down, `talk` would fall through to its own cold-path
   behavior and spawn a fresh headless `claude -p` session — from a wake
   word false positive, with no human in the loop. `daemon.py` now checks
   `bridge_session_exists()` (a plain `tmux has-session -t imessage-bridge`)
   immediately before every trigger. If the bridge isn't up, it does **not**
   invoke `talk` at all — it logs a warning and posts a macOS notification
   (`osascript -e 'display notification "wake word heard but bridge is
   down" with title "handsfree"'`) instead, and still starts the cooldown
   timer so a noisy false-positive run can't spam notifications either.

2. **Triggers could stack, and the fixed cooldown timer doesn't cover a
   long conversation.** The original cooldown (120s default) suppresses a
   *second* trigger shortly after the first, but a voice conversation can
   easily run longer than that — and if the assistant's own Kokoro TTS
   output leaks into the mic mid-conversation and gets heard as "hey
   jarvis" again, a cooldown that already expired would let a second
   `talk` invocation stack on top of the first. **To be explicit: the fix
   for this is the code below, not an assumption that Bluetooth echo
   cancellation handles it upstream of the mic.** (The live-capture
   verification gap documented further down is a *separate*, still-open
   question about whether the mic hears the wake phrase reliably at all —
   it has nothing to do with self-trigger safety, which is guaranteed by
   this guard regardless of what the mic hears.)

   Two signals combine into `conversation_presumed_active()`, checked
   *before* a frame is even scored (openWakeWord's `predict()` isn't
   called at all while either is true — this is "discard detections" taken
   literally, not just "don't act on them"):

   - **A short-TTL lock file** (`wakeword.trigger.lock`, default TTL 15s)
     written at the exact moment `talk` is invoked. This closes the race
     between sending the wake-up to the bridge and the bridge's tmux pane
     visibly showing a busy prompt (send-keys + Enter has some settle
     time — see `bin/talk`'s own comment on this).
   - **The bridge pane's own busy/idle state**, read via `tmux
     capture-pane -t imessage-bridge -p -e` and parsed with the *same*
     idle-detection logic `bin/talk` already uses (an empty prompt after
     `❯`, or only a dim/grey ANSI-suggested-prompt ghost, means idle;
     anything else means a turn — including a `voicemode converse`
     call — is in flight). This is the mechanism that actually covers "the
     conversation is still running after the cooldown timer expired": as
     long as the pane shows busy, frames aren't scored, full stop,
     regardless of how long that turn takes. Cached and re-checked once a
     second (`BRIDGE_BUSY_CHECK_INTERVAL`), not on every 80ms frame, so
     this doesn't itself become a CPU or `tmux`-spam problem.

   Both checks fail closed: if `tmux` can't be reached, times out, or the
   pane has no recognizable prompt line at all, the state is treated as
   busy (suppress), never as idle (permit).

Relevant code (`wakeword/daemon.py`):

```python
def bridge_session_exists(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def bridge_is_busy(session: str) -> bool:
    """Fails closed: any error talking to tmux, or no prompt line found,
    is treated as busy."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-e"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return True
    if result.returncode != 0:
        return True

    lines = result.stdout.splitlines()
    prompt_lines = [line for line in lines if "❯" in line]
    if not prompt_lines:
        return True

    match = re.search(r"❯\xa0?(.*)$", prompt_lines[-1])
    rest = (match.group(1) if match else "").strip("\xa0 ")
    idle = rest == "" or rest.startswith("\x1b[2m")
    return not idle
```

Main loop (self-trigger suppression happens *before* scoring; bounded
trigger happens *after* a real detection clears cooldown):

```python
while True:
    chunk = audio_q.get()

    # Self-trigger guard: don't even score this frame while a
    # conversation is presumed active.
    if trigger_lock_active(args.lock_path, args.lock_ttl) or busy_monitor.is_busy():
        continue

    try:
        prediction = oww_model.predict(chunk)
    except Exception:
        logging.exception("prediction failed on this frame -- skipping")
        continue

    score = float(prediction.get(model_label, 0.0))
    if score < args.threshold:
        continue

    now = datetime.datetime.now(datetime.timezone.utc)
    in_cooldown = last_trigger is not None and (now - last_trigger).total_seconds() < args.cooldown
    if in_cooldown:
        remaining = args.cooldown - (now - last_trigger).total_seconds()
        logging.info("detection score=%.3f suppressed (cooldown, %.0fs remaining)", score, remaining)
        continue

    # Bounded trigger path: only ever invoke `talk` when the warm
    # bridge actually exists.
    if not bridge_session_exists(args.bridge_session):
        notify_bridge_down(score, args.dry_run)
        last_trigger = now
        continue

    logging.info("detection score=%.3f -- triggering talk", score)
    write_trigger_lock(args.lock_path)
    trigger_talk(args.talk_bin, args.dry_run)
    last_trigger = now
```

## Thresholds and cooldown

- `--threshold` (default `0.5`): openWakeWord's own models are tuned for a
  `0.5` default; both stock-voice and `say`-synthesized test audio scored
  `0.94`–`0.9999` on true positives during local WAV testing, well clear of
  that bar.
- `--cooldown` (default `120s`): once triggered, further detections are
  logged as `suppressed (cooldown, Ns remaining)` rather than re-invoking
  `talk`. This is the *short-window* guard against a rapid repeat trigger;
  the *long-conversation* case is covered by the bridge-busy check above,
  not by making this number bigger. Set via `WAKEWORD_COOLDOWN` env var
  when calling `bin/wakeword`, or `--cooldown` directly against
  `daemon.py`.
- `--lock-ttl` (default `15s`): how long the trigger lock alone suppresses
  scoring after invoking `talk`, before the bridge-busy check takes over.
- `--bridge-session` (default `imessage-bridge`, or `$TALK_BRIDGE_SESSION`
  if set): must match whatever `bin/bridge`/`bin/talk` are using.
- `--dry-run`: logs detections (including cooldown-suppressed ones)
  without invoking `talk` or posting the bridge-down notification. Used
  for the manual checklist above.

## launchd + stop semantics (read before touching `bin/wakeword`)

`wakeword install-launchd` runs the daemon under `KeepAlive=true` — if it
exits for any reason (crash, mic error, a bare `kill`), launchd restarts
it within seconds. **A bare `kill <pid>` on a launchd-managed instance
does not stop it** — it gets resurrected almost immediately, which for an
always-on-mic daemon is a real privacy bug, not a cosmetic one.
`wakeword stop` accounts for this: it checks `launchctl list` for
`com.handsfree.wakeword` first and, if loaded, runs `launchctl unload`
(which both kills the current process and tells launchd to stop
restarting it) before falling back to killing any orphaned
manually-started process. `wakeword status` also reports whether the
launchd job is loaded, specifically so "why is the mic still on after I
killed it" isn't a mystery.

One implementation detail worth knowing if you touch this: the launchd
job's `ProgramArguments` runs `/bin/zsh -c "<venv-python> daemon.py ... ||
osascript ..."` (needed for the failure-alert `||`), which means the
*supervised* process launchd tracks is the `zsh -c` wrapper, and the real
`python3 daemon.py` process is its child. `pgrep -f` on the daemon's
script path will therefore match **both** PIDs while running under
launchd (verified: PPID chain is `launchd -> zsh -c ... -> python3
daemon.py`). This is fine — `launchctl unload` cleanly tears down the
whole tree (verified: both PIDs gone within ~1s of unload, no orphan) —
but don't "simplify" `stop()` to a bare `kill` on whatever `pgrep`
returns first; it might be the zsh wrapper, not the python process, and
killing only that would orphan the mic-holding python3 process to PID 1
while still leaking mic access.

## Two log locations, same content (mostly)

- `~/.local/state/handsfree/wakeword.log` — the daemon's own structured
  log (`INFO detection score=...`, etc.), written by `daemon.py` itself
  via `--log-path`. This is what `wakeword status` tails and what the
  manual checklist above watches with `tail -f`.
- `~/Library/Logs/handsfree/wakeword.out.log` /
  `wakeword.err.log` — launchd's `StandardOutPath`/`StandardErrorPath`.
  Since `daemon.py` also logs to stdout (`logging.StreamHandler(sys.stdout)`
  alongside the file handler), `wakeword.out.log` is effectively a
  duplicate of `wakeword.log`'s INFO lines, just scoped to whatever's run
  since the launchd job last (re)started. `wakeword.err.log` is the one
  that actually earns its keep: it's where a Python traceback (crash,
  unhandled exception outside the per-frame `try/except` around
  `predict()`) or the `osascript` failure-notification output lands —
  check it first when `wakeword status` shows the job bouncing.

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
conclusively the cause. **To be clear, this Bluetooth-AEC theory is a
hypothesis about mic reliability, not a safety mechanism — it is not, and
must never be treated as, the self-trigger guard.** That guard is the code
in the section above, and it works regardless of which theory (if either)
turns out to explain the live-capture gap.

Mic permission is **not** the blocker: `TCC.db` shows
`com.mitchellh.ghostty` (the terminal these tests ran from) already granted
`kTCCServiceMicrophone` (`auth_value=2`), and the daemon did capture real
audio successfully at least once (run 1's detection burst) — a true
permission block would produce zero captures ever, not an intermittent
one.

**What Sam should do to close this out** is now the Manual verification
checklist at the top of this file. The short version of what changed since
the first pass: test with the built-in mic + room speakers, not the
Bluetooth headset, and use your own voice instead of `say` (removes the
own-device echo-cancellation variable entirely). If that's a clean 5/5,
the Bluetooth theory is confirmed and the fix is either "don't wear the
headset while expecting wake-word to work" or investigating per-app audio
routing so `talk`'s TTS output and the wake-word mic input use different
devices. If it's still flaky on a non-Bluetooth setup, capture `daemon.py`'s
stderr during a failing trial (not just at shutdown) to see whether the
`AUHAL Error -50` recurs mid-stream.

**Cooldown ("exactly once per window"): verified.** Run 1's log showed the
mechanism working exactly as designed: the first above-threshold frame
logged `detection score=0.964 -- triggering talk`, and the next 13
consecutive above-threshold frames (one continuous utterance) all logged
`suppressed (cooldown, Ns remaining)` instead of triggering again. The
bounded-trigger and self-trigger-guard code added in the review-fix round
above wasn't part of that original run (it postdates it) but reuses the
identical scoring/cooldown code path, unit-tested separately (see
`bridge_session_exists`/`bridge_is_busy` behavior against a live tmux
session, and the idle/busy parsing logic in isolation) rather than via
speaker playback, per the no-more-audio-testing constraint this round
worked under.

CPU (below) was verified without requiring speaker playback and stands
as-is.

## CPU

Idle (`--dry-run`, mic open, no detections) measured at **~4.6% of one
core** via `top -pid <pid> -s 3` over six 3-second samples on this M-series
Mac — under the 5% budget, if close to it. Most of that is the
melspectrogram/embedding/classifier inference running once per 80ms frame
continuously; there's no larger duty-cycle knob in openWakeWord to trade
latency for less-frequent inference without vendoring the prediction loop.
The self-trigger guard should *lower* average CPU further during actual
conversations (frames are discarded before inference runs at all), though
that wasn't separately re-measured this round.

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
tmux session. See "launchd + stop semantics" above before changing `stop()`.
