#!/usr/bin/env python3
"""Local wake-word daemon: mic -> openWakeWord -> ~/.local/bin/talk.

Listens continuously on the default input device for the wake phrase and,
on detection, invokes the existing `talk` warm path (which itself prefers
the persistent imessage-bridge tmux session when it's up). This drops the
"Hey Siri, talk to Claude" hop for anyone at the machine.

Usage:
    daemon.py [--model PATH] [--threshold 0.5] [--cooldown 120]
              [--dry-run] [--device NAME_OR_INDEX] [--log-path PATH]

See wakeword/README.md for the model choice, the onnxruntime-vs-tflite
gotcha this file works around, and the custom-model upgrade path.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import queue
import subprocess
import sys
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

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80ms at 16kHz -- openWakeWord's native chunk size


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
        "starting wakeword daemon: model=%s threshold=%.2f cooldown=%ss dry_run=%s device=%s",
        model_label, args.threshold, args.cooldown, args.dry_run, args.device or "default",
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
            prediction = oww_model.predict(chunk)
            score = float(prediction.get(model_label, 0.0))

            if score >= args.threshold:
                now = datetime.datetime.now(datetime.timezone.utc)
                in_cooldown = last_trigger is not None and (now - last_trigger).total_seconds() < args.cooldown
                if in_cooldown:
                    remaining = args.cooldown - (now - last_trigger).total_seconds()
                    logging.info("detection score=%.3f suppressed (cooldown, %.0fs remaining)", score, remaining)
                    continue

                logging.info("detection score=%.3f -- triggering talk", score)
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
    parser.add_argument("--dry-run", action="store_true", help="Log detections but do not invoke talk")
    parser.add_argument("--device", default=None, help="Input device name or index (default: system default input)")
    parser.add_argument("--talk-bin", type=Path, default=DEFAULT_TALK_BIN, help=f"Path to invoke on detection (default: {DEFAULT_TALK_BIN})")
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH, help=f"Log file path (default: {DEFAULT_LOG_PATH})")
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
