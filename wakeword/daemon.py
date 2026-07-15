#!/usr/bin/env python3
"""Local wake-word daemon: mic -> openWakeWord -> ~/.local/bin/talk.

Listens continuously on the default input device for the wake phrase and,
on detection, invokes the existing `talk` warm path -- but only when the
persistent imessage-bridge tmux session actually exists (see
`bridge_session_exists`) and isn't already mid-conversation (see
`conversation_presumed_active`). This drops the "Hey Siri, talk to Claude"
hop for anyone at the machine without being able to spawn an unbounded
headless session on a false positive, or stack a second trigger on top of
an ongoing one.

Usage:
    daemon.py [--model PATH] [--threshold 0.5] [--cooldown 120]
              [--dry-run] [--device NAME_OR_INDEX] [--log-path PATH]
              [--bridge-session NAME] [--lock-ttl 15]

See wakeword/README.md for the model choice, the onnxruntime-vs-tflite
gotcha this file works around, the bounded-trigger design, and the
custom-model upgrade path.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import queue
import re
import subprocess
import sys
import time
import types
from pathlib import Path

import numpy as np
import sounddevice as sd

# --- tflite shim -----------------------------------------------------------
#
# openWakeWord's tflite path imports `tflite_runtime.interpreter`, but
# `tflite-runtime` has no macOS wheel at all (confirmed 2026-07-14: uv/pip
# only offer manylinux/armv7l wheels for it). `ai-edge-litert` is Google's
# actively maintained successor with an API-compatible `Interpreter` class,
# and it does ship macOS ARM64 wheels. Register it under the name
# openWakeWord expects before importing the model module.
#
# This isn't cosmetic: the .onnx release of these same models produces
# near-zero scores for genuine positive audio on this machine (verified
# against openWakeWord's own bundled test clips — max score ~0.0001 via
# onnxruntime 1.27.0 vs ~0.999 via this tflite path for identical audio).
# Root cause not identified (likely an onnx export/opset mismatch upstream,
# not an environment misconfiguration — the onnx pipeline runs without
# error and produces plausible-shaped, just badly-scaled, output). Use
# tflite here; don't "simplify" this back to onnx without re-verifying
# against a known positive clip first.
def _install_tflite_shim() -> None:
    import ai_edge_litert.interpreter as litert

    tflite_pkg = types.ModuleType("tflite_runtime")
    tflite_interp = types.ModuleType("tflite_runtime.interpreter")
    tflite_interp.Interpreter = litert.Interpreter
    tflite_interp.load_delegate = litert.load_delegate
    sys.modules.setdefault("tflite_runtime", tflite_pkg)
    sys.modules.setdefault("tflite_runtime.interpreter", tflite_interp)


_install_tflite_shim()

import openwakeword  # noqa: E402
from openwakeword.model import Model  # noqa: E402

WAKEWORD_DIR = Path(__file__).resolve().parent
MODELS_DIR = WAKEWORD_DIR / "models"
DEFAULT_MODEL_NAME = "hey_jarvis"
DEFAULT_TALK_BIN = Path.home() / ".local" / "bin" / "talk"
DEFAULT_LOG_PATH = Path.home() / ".local" / "state" / "handsfree" / "wakeword.log"
DEFAULT_LOCK_PATH = Path.home() / ".local" / "state" / "handsfree" / "wakeword.trigger.lock"

# Matches bin/talk's TALK_BRIDGE_SESSION env var so both pieces stay in
# sync if Sam ever renames the bridge session.
DEFAULT_BRIDGE_SESSION = os.environ.get("TALK_BRIDGE_SESSION", "imessage-bridge")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80ms at 16kHz -- openWakeWord's native chunk size

# How often to re-check the bridge pane's busy/idle state via `tmux
# capture-pane` while suppressing detection (blocker: self-trigger guard).
# A subprocess spawn every 80ms frame would be wasteful and could itself
# eat into the CPU budget; once a second is frequent enough that a
# conversation ending is noticed promptly without hammering tmux.
BRIDGE_BUSY_CHECK_INTERVAL = 1.0


def ensure_model_files(model_name: str) -> tuple[Path, Path, Path]:
    """Download the tflite model trio for `model_name` if not already local."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    wakeword_path = MODELS_DIR / f"{model_name}_v0.1.tflite"
    melspec_path = MODELS_DIR / "melspectrogram.tflite"
    embedding_path = MODELS_DIR / "embedding_model.tflite"

    if not (wakeword_path.exists() and melspec_path.exists() and embedding_path.exists()):
        logging.info("downloading wake-word models to %s (first run only)", MODELS_DIR)
        openwakeword.utils.download_models(model_names=[model_name], target_directory=str(MODELS_DIR))

    return wakeword_path, melspec_path, embedding_path


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )


# --- bounded trigger path ---------------------------------------------------
#
# Three separate guards, all of which must clear before `talk` is invoked:
#
# 1. bridge_session_exists() -- the tmux session that backs the warm path
#    must actually be up. If it isn't, we do NOT fall through to a cold
#    `claude -p` spawn on a wake-word false positive (that was the
#    unbounded-spawn risk flagged in review) -- we notify and stop.
# 2. conversation_presumed_active() -- a short-TTL lock file written at the
#    moment of trigger (covers the race between invoking talk and the
#    bridge pane visibly going busy) OR the bridge pane already showing a
#    non-idle prompt (covers a long-running conversation, including one
#    the cooldown timer has already expired during). Either signal being
#    true means: don't even score this frame, let alone trigger.
# 3. the ordinary cooldown timer, for everything the above two don't cover
#    (repeated benign wake-word hits shortly after a completed trigger).
def bridge_session_exists(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def bridge_is_busy(session: str) -> bool:
    """True if the bridge tmux pane's prompt is non-empty (a turn is in flight).

    Ports bin/talk's BRIDGE_IDLE detection verbatim in spirit: the REPL
    renders an empty prompt as "❯" with nothing (or only a non-breaking
    space) after it, or a dim/grey *suggested* next prompt (ANSI SGR
    \\x1b[2m) that is not real input. Anything else after the prompt marker
    means Claude is mid-turn -- a voicemode converse call, a long response
    rendering, etc. -- which is exactly the self-trigger window this exists
    to detect: the assistant's own Kokoro voice leaking into the mic must
    not be able to re-trigger `talk` just because the fixed cooldown timer
    happened to expire while that turn was still running.

    Fails closed *given a session that exists*: any error talking to tmux
    (timeout, session vanished mid-check, no prompt line found at all) is
    treated as busy, so an unreadable pane suppresses a trigger rather than
    risking a stacked one.

    Deliberately NOT fail-closed when the session doesn't exist at all --
    "busy" means "a conversation is in flight," and there's nothing in
    flight if there's no bridge session. Treating "absent" as "busy" would
    permanently suppress scoring (and therefore detection) any time the
    bridge is down, which defeats the whole point of the bounded-trigger
    path: a wake word heard while the bridge is down needs to actually be
    scored so it can produce the "bridge is down" notification, not get
    silently discarded here first. `bridge_session_exists()` at the actual
    trigger point is what decides whether to invoke `talk` vs. notify.
    """
    if not bridge_session_exists(session):
        return False

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


class BridgeBusyMonitor:
    """Caches bridge_is_busy() so the main loop isn't spawning `tmux
    capture-pane` on every 80ms audio frame."""

    def __init__(self, session: str, check_interval: float = BRIDGE_BUSY_CHECK_INTERVAL) -> None:
        self.session = session
        self.check_interval = check_interval
        self._value = False
        self._checked_at = 0.0

    def is_busy(self) -> bool:
        now = time.monotonic()
        if now - self._checked_at >= self.check_interval:
            self._value = bridge_is_busy(self.session)
            self._checked_at = now
        return self._value


def trigger_lock_active(lock_path: Path, ttl_seconds: float) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return age < ttl_seconds


def write_trigger_lock(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(datetime.datetime.now(datetime.timezone.utc).isoformat())


def notify_bridge_down(score: float, dry_run: bool) -> None:
    logging.warning("detection score=%.3f but bridge is down -- not spawning a cold session", score)
    if dry_run:
        logging.info("[dry-run] would post macOS notification (bridge down)")
        return
    try:
        subprocess.run(
            ["osascript", "-e", 'display notification "wake word heard but bridge is down" with title "handsfree"'],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
    except OSError as exc:
        logging.error("failed to post notification: %s", exc)


def trigger_talk(talk_bin: Path, dry_run: bool) -> None:
    if dry_run:
        logging.info("[dry-run] would invoke %s", talk_bin)
        return
    if not talk_bin.exists():
        logging.error("talk binary not found at %s -- detection logged, nothing invoked", talk_bin)
        return
    try:
        subprocess.Popen(
            [str(talk_bin)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logging.info("invoked %s", talk_bin)
    except OSError as exc:
        logging.error("failed to invoke %s: %s", talk_bin, exc)


def run(args: argparse.Namespace) -> int:
    setup_logging(args.log_path)

    if args.model and Path(args.model).exists():
        wakeword_path = Path(args.model)
        melspec_path = MODELS_DIR / "melspectrogram.tflite"
        embedding_path = MODELS_DIR / "embedding_model.tflite"
        if not (melspec_path.exists() and embedding_path.exists()):
            _, melspec_path, embedding_path = ensure_model_files(DEFAULT_MODEL_NAME)
    else:
        model_name = args.model or DEFAULT_MODEL_NAME
        wakeword_path, melspec_path, embedding_path = ensure_model_files(model_name)

    model_label = wakeword_path.stem  # e.g. "hey_jarvis_v0.1"

    logging.info(
        "starting wakeword daemon: model=%s threshold=%.2f cooldown=%ss dry_run=%s device=%s "
        "bridge_session=%s lock_ttl=%ss",
        model_label, args.threshold, args.cooldown, args.dry_run, args.device or "default",
        args.bridge_session, args.lock_ttl,
    )

    oww_model = Model(
        wakeword_models=[str(wakeword_path)],
        inference_framework="tflite",
        melspec_model_path=str(melspec_path),
        embedding_model_path=str(embedding_path),
    )

    audio_q: "queue.Queue[np.ndarray]" = queue.Queue()

    def audio_callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            logging.warning("audio input status: %s", status)
        audio_q.put(indata[:, 0].copy())

    last_trigger: datetime.datetime | None = None
    busy_monitor = BridgeBusyMonitor(args.bridge_session)

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=args.device,
            callback=audio_callback,
        )
    except Exception:
        logging.exception(
            "could not open microphone input. If this is the first run, macOS may be "
            "waiting on a TCC microphone-permission prompt attributed to the process "
            "that launched this daemon (Terminal, python3, or launchd's own binary) -- "
            "check System Settings > Privacy & Security > Microphone and enable it there, "
            "then restart the daemon."
        )
        return 1

    with stream:
        logging.info("listening")
        while True:
            chunk = audio_q.get()

            # Self-trigger guard: while a conversation is presumed active
            # (fresh trigger lock, or the bridge pane itself shows a
            # non-idle prompt), don't even score this frame. This is the
            # actual guard against the assistant's own Kokoro voice
            # re-triggering wake-word detection mid-conversation -- NOT an
            # assumption that Bluetooth echo-cancellation handles it
            # upstream of the mic. See "Self-trigger guard" in README.
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
            # bridge actually exists. A wake-word false positive with the
            # bridge down must not fall through to an unbounded cold
            # `claude -p` spawn -- notify instead and stop here.
            if not bridge_session_exists(args.bridge_session):
                notify_bridge_down(score, args.dry_run)
                last_trigger = now  # still starts cooldown, so a noisy false-positive can't spam notifications
                continue

            logging.info("detection score=%.3f -- triggering talk", score)
            write_trigger_lock(args.lock_path)
            trigger_talk(args.talk_bin, args.dry_run)
            last_trigger = now


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", default=None,
        help="Pretrained model name (default: hey_jarvis) or a path to a .tflite file",
    )
    parser.add_argument("--threshold", type=float, default=0.5, help="Detection score threshold (default: 0.5)")
    parser.add_argument("--cooldown", type=float, default=120, help="Seconds to suppress re-trigger after a detection (default: 120)")
    parser.add_argument("--dry-run", action="store_true", help="Log detections but do not invoke talk or post notifications")
    parser.add_argument("--device", default=None, help="Input device name or index (default: system default input)")
    parser.add_argument("--talk-bin", type=Path, default=DEFAULT_TALK_BIN, help=f"Path to invoke on detection (default: {DEFAULT_TALK_BIN})")
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH, help=f"Log file path (default: {DEFAULT_LOG_PATH})")
    parser.add_argument(
        "--bridge-session", default=DEFAULT_BRIDGE_SESSION,
        help=f"tmux session name for the warm bridge (default: {DEFAULT_BRIDGE_SESSION}, or $TALK_BRIDGE_SESSION)",
    )
    parser.add_argument(
        "--lock-path", type=Path, default=DEFAULT_LOCK_PATH,
        help=f"Trigger lock file path (default: {DEFAULT_LOCK_PATH})",
    )
    parser.add_argument(
        "--lock-ttl", type=float, default=15,
        help="Seconds the trigger lock suppresses detection after invoking talk, bridging the gap "
             "before the bridge pane visibly shows busy (default: 15)",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        logging.info("stopped (KeyboardInterrupt)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
