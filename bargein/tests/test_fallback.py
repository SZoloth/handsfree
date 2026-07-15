from bargein.routing import tool_preference_prompt


def test_daemon_down_prompt_keeps_plain_voicemode_fallback():
    prompt = tool_preference_prompt(daemon_available=False)

    assert "voicemode converse" in prompt
    assert "handsfree" not in prompt.lower()


def test_daemon_up_prompt_prefers_barge_in_tool_and_names_fallback():
    prompt = tool_preference_prompt(daemon_available=True)

    assert "handsfree speak_and_listen" in prompt
    assert "voicemode converse" in prompt
