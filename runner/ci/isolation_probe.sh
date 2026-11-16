#!/bin/sh
# Behavioural guard for AI-04/AI-07, run inside the runner image by CI (no network, no
# credential needed): a SessionStart hook in the workspace's .claude/settings.json must
# NOT run when the CLI is started the way the bridge starts it (empty setting sources),
# and MUST run with `--setting-sources=project` (control: proves the probe can see it).
# Independent of how the SDK spells the flag — test_hardening.py covers that side.
set -u
fail=0
probe() {   # probe <label> <expected: yes|no> [cli args...]
  label=$1; expected=$2; shift 2
  d=$(mktemp -d); mkdir -p "$d/.claude"
  printf '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"touch %s/HOOK_RAN"}]}]}}\n' "$d" > "$d/.claude/settings.json"
  ( cd "$d" && HOME="$d" ANTHROPIC_API_KEY=dummy-key-not-real ANTHROPIC_BASE_URL=http://127.0.0.1:9 \
      timeout "${PROBE_TIMEOUT:-30}" claude -p hi "$@" > /dev/null 2>&1 )
  if [ -e "$d/HOOK_RAN" ]; then ran=yes; else ran=no; fi
  echo "isolation probe: $label -> workspace hook ran: $ran (expected $expected)"
  [ "$ran" = "$expected" ] || fail=1
}
probe "control --setting-sources=project" yes --setting-sources=project
probe "--setting-sources= (SDK form)"      no  --setting-sources=
probe "--setting-sources \"\" (two tokens)" no  --setting-sources ""
exit $fail
