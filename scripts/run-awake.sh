#!/usr/bin/env bash
#
# Run shadow-ai so it keeps serving Slack even when the laptop is LOCKED and the
# LID IS CLOSED.
#
# Three independent things must be true for the bot to survive a closed lid:
#
#   1. The process stays awake.  `caffeinate -s` asserts PreventSystemSleep,
#      honoured even on battery.  We run the bot AS A CHILD of caffeinate, so
#      caffeinate exits the moment the bot exits — no orphaned power assertions.
#
#   2. Closing the lid must not suspend the machine (and with it, networking).
#      caffeinate CANNOT stop clamshell sleep; only `sudo pmset -a disablesleep 1`
#      can.  Without it, closing the lid suspends Wi-Fi and Socket Mode dies with
#      BrokenPipeError / DNS "nodename nor servname" errors even though the CPU
#      is still awake.  THIS is the step that was missing.  This script sets it
#      by default and reverts it when the bot stops.
#
#   3. AC power.  With disablesleep the Mac never sleeps, so on battery it will
#      drain; lid-closed Wi-Fi is also unreliable on battery.  Keep it plugged in.
#
# The sudo prompt appears ONCE at startup — answer it, then you can close the lid.
#
# Usage:
#   scripts/run-awake.sh              # keep-awake + survive closed lid (default)
#   NO_DISABLESLEEP=1 scripts/run-awake.sh   # caffeinate only, lid must stay OPEN
#
set -euo pipefail

cd "$(dirname "$0")/.."

# Prefer the venv's console script if present, else module form.
if [[ -x ".venv/bin/shadow-ai" ]]; then
  BOT_CMD=(.venv/bin/shadow-ai)
elif [[ -x ".venv/bin/python" ]]; then
  BOT_CMD=(.venv/bin/python -m shadow_ai)
else
  BOT_CMD=(python3 -m shadow_ai)
fi

DISABLED_CLAMSHELL=0

cleanup() {
  # Re-enable normal sleep if we disabled it, so the machine isn't left
  # permanently unable to sleep after the bot stops.
  if [[ "$DISABLED_CLAMSHELL" == "1" ]]; then
    echo "[run-awake] Re-enabling sleep (pmset -a disablesleep 0)…"
    sudo pmset -a disablesleep 0 || \
      echo "[run-awake] WARNING: could not revert; run 'sudo pmset -a disablesleep 0' manually." >&2
  fi
}
trap cleanup EXIT INT TERM

if [[ "${NO_DISABLESLEEP:-0}" != "1" ]]; then
  # Warn if on battery — disablesleep on battery drains the machine and
  # lid-closed Wi-Fi is unreliable there anyway.
  if pmset -g batt 2>/dev/null | grep -q "'Battery Power'"; then
    echo "[run-awake] WARNING: running on BATTERY. Plug into AC power — with sleep"
    echo "[run-awake]          disabled the Mac won't sleep and the battery will drain," >&2
    echo "[run-awake]          and lid-closed Wi-Fi is unreliable on battery." >&2
  fi

  echo "[run-awake] Disabling sleep so a closed lid won't suspend networking."
  echo "[run-awake] You'll be asked for your password once (reverted when the bot stops)."
  if sudo pmset -a disablesleep 1; then
    # Verify it actually took — a silent no-op here is what caused earlier
    # BrokenPipeError loops (SleepDisabled stayed 0).
    if pmset -g | grep -qE "SleepDisabled[[:space:]]+1"; then
      DISABLED_CLAMSHELL=1
      echo "[run-awake] OK: SleepDisabled=1. You can close the lid now."
    else
      echo "[run-awake] ERROR: pmset ran but SleepDisabled is still 0 — the lid will" >&2
      echo "[run-awake]        suspend networking. Aborting so you don't get a silent fail." >&2
      exit 1
    fi
  else
    echo "[run-awake] ERROR: 'sudo pmset -a disablesleep 1' failed (wrong password / no sudo)." >&2
    echo "[run-awake]        The bot would drop its Slack connection on lid close. Aborting." >&2
    exit 1
  fi
else
  echo "[run-awake] NO_DISABLESLEEP=1 — caffeinate only. Keep the lid OPEN."
fi

echo "[run-awake] Starting: caffeinate -s -i -m ${BOT_CMD[*]}"
echo "[run-awake] Ctrl-C to stop (sleep setting is reverted automatically)."

# -s prevent system sleep (honoured on battery)
# -i prevent idle system sleep
# -m prevent disk sleep
# caffeinate waits on the child; both die together.
exec caffeinate -s -i -m "${BOT_CMD[@]}"
