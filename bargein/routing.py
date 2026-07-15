def tool_preference_prompt(*, daemon_available: bool) -> str:
    if not daemon_available:
        return "Start a voice conversation with me now via voicemode converse."
    return (
        "Start a voice conversation with me now. Prefer handsfree speak_and_listen "
        "for each spoken turn; if it is unavailable, use voicemode converse."
    )
