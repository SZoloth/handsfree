import asyncio

from bargein import audio_helper
from bargein.audio_helper import HelperPlayback


def test_helper_starts_in_its_own_process_group(monkeypatch, tmp_path):
    async def run():
        executable = tmp_path / "handsfree-audio-helper"
        executable.touch()
        captured = {}

        class FakeProcess:
            pid = 4242

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
        monkeypatch.setattr(audio_helper, "_install_signal_cleanup_handlers", lambda: None)

        playback = HelperPlayback(helper_path=executable)
        try:
            await playback._start()
            assert captured["kwargs"]["start_new_session"] is True
            assert 4242 in audio_helper._LIVE_HELPER_GROUPS
        finally:
            with audio_helper._LIVE_HELPER_GROUPS_LOCK:
                audio_helper._LIVE_HELPER_GROUPS.discard(4242)

    asyncio.run(run())


def test_exit_cleanup_terminates_then_kills_live_helper_group(monkeypatch):
    signals = []
    monkeypatch.setattr(audio_helper, "_signal_helper_group", lambda group, sig: signals.append((group, sig)))

    with audio_helper._LIVE_HELPER_GROUPS_LOCK:
        audio_helper._LIVE_HELPER_GROUPS.add(4242)
    try:
        audio_helper._terminate_live_helper_groups(force=True)
    finally:
        with audio_helper._LIVE_HELPER_GROUPS_LOCK:
            audio_helper._LIVE_HELPER_GROUPS.discard(4242)

    assert signals == [
        (4242, audio_helper.signal.SIGTERM),
        (4242, audio_helper.signal.SIGKILL),
    ]
