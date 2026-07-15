#!/bin/zsh
# handsfree installer — idempotent. Run from the repo root.
# Sets up local voice services, registers the VoiceMode MCP, links the CLIs,
# and imports the Siri shortcut. Safe to re-run.

set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
say_step() { print -P "%F{cyan}==> $1%f" }

say_step "Checking dependencies (claude, uv, tmux, ffmpeg)"
for dep in claude uvx tmux ffmpeg; do
  command -v "$dep" >/dev/null || { echo "MISSING: $dep — install it first"; exit 1; }
done

say_step "Registering VoiceMode MCP at user scope (all Claude Code sessions)"
if claude mcp list 2>/dev/null | grep -q "^voicemode"; then
  echo "    already registered"
else
  claude mcp add --scope user voicemode -- uvx --from voice-mode voicemode
fi

say_step "Installing Whisper STT (port 2022) and Kokoro TTS (port 8880)"
uvx --from voice-mode voicemode service install whisper || true
uvx --from voice-mode voicemode service install kokoro  || true

say_step "Upgrading Whisper ears to large-v3-turbo (the base model mishears badly)"
uvx --from voice-mode voicemode whisper model large-v3-turbo || true

say_step "Configuring: earcons on, af_sky voice identity, local-first"
uvx --from voice-mode voicemode config set VOICEMODE_AUDIO_FEEDBACK true
uvx --from voice-mode voicemode config set VOICEMODE_KOKORO_DEFAULT_VOICE af_sky
uvx --from voice-mode voicemode config set VOICEMODE_VOICES af_sky
uvx --from voice-mode voicemode config set VOICEMODE_PREFER_LOCAL true
uvx --from voice-mode voicemode config set VOICEMODE_AUTO_START_KOKORO true
# Root-caused 2026-07-14: the default of 25 (voicemode's own memory-leak
# mitigation for kokoro-fastapi, see github.com/hexgrad/kokoro/issues/152)
# is exhausted by unrelated /api/health polling noise within 1-2 minutes,
# and launchd's KeepAlive=true treats every resulting clean shutdown as a
# crash and immediately restarts it — 1,058 restarts in 31 days serving
# zero real TTS traffic. Raised so the mitigation still fires eventually
# under real load, but health-check noise can't trigger a restart mid-session.
uvx --from voice-mode voicemode config set VOICEMODE_KOKORO_MAX_REQUESTS 2000

say_step "Starting services and enabling auto-start at login"
uvx --from voice-mode voicemode service start whisper  || true
uvx --from voice-mode voicemode service start kokoro   || true
uvx --from voice-mode voicemode service enable whisper || true
uvx --from voice-mode voicemode service enable kokoro  || true

say_step "Linking talk + bridge into ~/.local/bin"
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO/bin/talk"   "$HOME/.local/bin/talk"
ln -sf "$REPO/bin/bridge" "$HOME/.local/bin/bridge"
chmod +x "$REPO/bin/talk" "$REPO/bin/bridge"

say_step "Importing 'Talk to Claude' Siri shortcut"
if shortcuts list 2>/dev/null | grep -q "Talk to Claude"; then
  echo "    already installed"
else
  open "$REPO/shortcuts/Talk to Claude.shortcut"
  echo "    >> Click 'Add Shortcut' in the dialog that just opened."
  echo "    >> Then in Shortcuts: Settings -> Advanced -> enable 'Allow Running Scripts'."
fi

say_step "Verifying the voice loop (TTS -> STT round trip, no mic needed)"
sleep 2
if curl -s --max-time 5 http://127.0.0.1:8880/v1/audio/speech \
     -H "Content-Type: application/json" \
     -d '{"model":"kokoro","input":"Handsfree install verified.","voice":"af_sky","response_format":"wav"}' \
     -o /tmp/handsfree-verify.wav \
   && curl -s --max-time 10 http://127.0.0.1:2022/v1/audio/transcriptions \
     -F file=@/tmp/handsfree-verify.wav -F model=whisper-1 | grep -qi "verified"; then
  echo "    round trip OK: Kokoro spoke, Whisper transcribed"
else
  echo "    round trip FAILED — services may still be warming up; re-run:"
  echo "    uvx --from voice-mode voicemode service status"
fi

say_step "Manual steps that remain"
cat <<'EOF'
  1. Add the two contracts from contracts/voice-contracts.md to ~/CLAUDE.md
     (edit the phone number placeholder). They govern how Claude behaves by
     voice and over iMessage.
  2. iMessage channel: inside a claude session run
        /plugin install imessage@claude-plugins-official
     grant Full Disk Access when prompted, then:
        bridge start && bridge install-launchd
     (install-launchd adds reboot survival: a launchd watchdog that
     re-creates the bridge tmux session if it's ever absent, checked every
     5 minutes. It's a plugin-install-gated manual step, not part of the
     automated install above, because bridge start fails until the
     iMessage plugin from step 2 is in place.)
  3. First mic use pops a macOS microphone permission prompt — click Allow.
  4. iPhone: Settings -> Notifications -> Announce Notifications -> Messages ON
     (makes bridge replies fully eyes-free on AirPods).
  5. Say "Hey Siri, talk to Claude" once; approve the first-run prompt if asked.
EOF
echo "Done. Try: talk \"what can you do by voice?\""
