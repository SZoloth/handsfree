---
name: pstack-project
description: Apply pstack to Handsfree engineering, using the existing project controls and evidence boundaries.
---

# Handsfree

Use `/Users/samzoloth/.codex/skills/pstack-workflow/SKILL.md`. Read README.md and TESTING.md. Trace the affected code in `bargein`, `wakeword` or `Sources` and the matching tests before selecting commands.

## Verification route

Use static/package checks and isolated tests that do not acquire audio devices. Inspect scripts before running: wake-word dry runs can still open the microphone.

TESTING.md owns the human acoustic acceptance sequence. Spoken startup, interruption, wake-word false positives, output route and recovery require actual hearing/device proof; a Swift/Python build does not supply it.

Do not run install.sh, launch agents, the resident daemon, microphone tests or Messages integrations as setup checks. Preserve existing owned audio processes.

This is a source-grounded engineering route, not a certified end-to-end verifier. Record fresh results and remaining gaps for the feature actually changed.
