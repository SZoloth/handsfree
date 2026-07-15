import struct

from bargein.vad import SILERO_SAMPLE_RATE, SILERO_WINDOW_SAMPLES, SileroVAD


class Probability:
    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value


class ModelSpy:
    def __init__(self, probability):
        self.probability = probability
        self.calls = []
        self.reset_count = 0

    def __call__(self, samples, sample_rate):
        self.calls.append((samples, sample_rate))
        return Probability(self.probability)

    def reset_states(self):
        self.reset_count += 1


def test_silero_adapter_sends_512_normalized_samples_at_16khz():
    model = ModelSpy(0.8)
    vad = SileroVAD(
        threshold=0.55,
        model=model,
        tensor_factory=lambda samples: [sample / 32768.0 for sample in samples],
    )
    pcm = b"".join(struct.pack("<h", 16_384) for _ in range(SILERO_WINDOW_SAMPLES))

    assert vad.is_speech(pcm, SILERO_SAMPLE_RATE) is True
    assert len(model.calls[0][0]) == SILERO_WINDOW_SAMPLES
    assert model.calls[0][0][0] == 0.5
    assert model.calls[0][1] == SILERO_SAMPLE_RATE


def test_silero_adapter_resets_model_state_between_utterances():
    model = ModelSpy(0.1)
    vad = SileroVAD(model=model, tensor_factory=lambda samples: samples)

    vad.reset()

    assert model.reset_count == 1
