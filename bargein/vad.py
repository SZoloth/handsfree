from __future__ import annotations

import array
from collections.abc import Callable
from typing import Protocol


SILERO_SAMPLE_RATE = 16_000
SILERO_WINDOW_SAMPLES = 512


class VoiceActivityDetector(Protocol):
    def is_speech(self, pcm16: bytes, sample_rate: int) -> bool: ...


class PCM16Resampler:
    """Small streaming mono resampler used before Silero's fixed 16 kHz input."""

    def __init__(self, output_rate: int = SILERO_SAMPLE_RATE):
        self.output_rate = output_rate
        self._input_rate: int | None = None
        self._position = 0.0
        self._tail: int | None = None

    def reset(self) -> None:
        self._input_rate = None
        self._position = 0.0
        self._tail = None

    def process(self, pcm16: bytes, input_rate: int) -> list[int]:
        samples = array.array("h")
        samples.frombytes(pcm16)
        if not samples:
            return []
        if input_rate == self.output_rate:
            if self._input_rate != input_rate:
                self.reset()
                self._input_rate = input_rate
            return list(samples)
        if self._input_rate != input_rate:
            self.reset()
            self._input_rate = input_rate
        source = ([self._tail] if self._tail is not None else []) + list(samples)
        if len(source) < 2:
            self._tail = source[-1]
            return []
        step = input_rate / self.output_rate
        output: list[int] = []
        while self._position + 1 < len(source):
            left = int(self._position)
            fraction = self._position - left
            sample = source[left] + (source[left + 1] - source[left]) * fraction
            output.append(int(sample))
            self._position += step
        self._position -= len(source) - 1
        self._tail = source[-1]
        return output


class SileroVAD:
    """Stateful Silero ONNX detector using the upstream 512-sample wrapper."""

    def __init__(self, threshold: float = 0.55, model=None, tensor_factory=None):
        if model is None:
            from silero_vad import load_silero_vad

            model = load_silero_vad(onnx=True, opset_version=16)
        self._model = model
        self._tensor_factory = tensor_factory
        self.threshold = threshold
        self._resampler = PCM16Resampler()
        self._pending: list[int] = []

    def reset(self) -> None:
        self._pending.clear()
        self._resampler.reset()
        reset = getattr(self._model, "reset_states", None)
        if reset is not None:
            reset()

    def is_speech(self, pcm16: bytes, sample_rate: int) -> bool:
        self._pending.extend(self._resampler.process(pcm16, sample_rate))
        detected = False
        while len(self._pending) >= SILERO_WINDOW_SAMPLES:
            window = self._pending[:SILERO_WINDOW_SAMPLES]
            del self._pending[:SILERO_WINDOW_SAMPLES]
            probability = self._probability(window)
            detected = detected or probability >= self.threshold
        return detected

    def _probability(self, samples: list[int]) -> float:
        if self._tensor_factory is None:
            import torch

            normalized = torch.tensor(samples, dtype=torch.float32) / 32768.0
        else:
            normalized = self._tensor_factory(samples)
        return float(self._model(normalized, SILERO_SAMPLE_RATE).item())


class DebouncedSpeechDetector:
    def __init__(
        self,
        vad: VoiceActivityDetector,
        *,
        consecutive_frames: int = 5,
        on_probability: Callable[[bool], None] | None = None,
    ):
        if consecutive_frames < 1:
            raise ValueError("consecutive_frames must be positive")
        self.vad = vad
        self.consecutive_frames = consecutive_frames
        self.on_probability = on_probability
        self._voiced = 0
        self.triggered = False
        self.last_voiced = False

    def observe(self, pcm16: bytes, sample_rate: int) -> bool:
        if self.triggered:
            return True
        voiced = self.vad.is_speech(pcm16, sample_rate)
        self.last_voiced = voiced
        if self.on_probability is not None:
            self.on_probability(voiced)
        self._voiced = self._voiced + 1 if voiced else 0
        self.triggered = self._voiced >= self.consecutive_frames
        return self.triggered

    def reset(self) -> None:
        self._voiced = 0
        self.triggered = False
        self.last_voiced = False
        reset = getattr(self.vad, "reset", None)
        if reset is not None:
            reset()
