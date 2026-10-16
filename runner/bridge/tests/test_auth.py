"""The login flow against a stand-in CLI that mimics `claude setup-token` 2.1.301.

The stand-in reproduces the verified behaviour: needs a TTY, prints an
authorization URL, waits for a pasted code, prints "OAuth error: Invalid code …"
on a bad code and a `sk-ant-oat01-…` token on a good one.
"""

import asyncio
import stat
import sys
from pathlib import Path

import pytest

from bridge.auth import AuthFlowError, AuthFlowManager, AuthFlowPending, scrub

FAKE_TOKEN = "sk-ant-oat01-" + "A1b2C3d4" * 6

FAKE_CLI = f'''#!{sys.executable}
import sys, time
if sys.argv[1:] != ["setup-token"]:
    sys.exit(2)
if not sys.stdin.isatty():
    print("")
    time.sleep(30)
    sys.exit(1)
mode = "{{mode}}"
if mode == "silent":
    sys.exit(0)
print("\\x1b[1mWelcome to Claude Code v2.1.301\\x1b[0m")
print("Browser didn't open? Use the url below to sign in (c to copy)")
print("")
print("https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz")
print("")
print("Paste code here if prompted >", end="", flush=True)
if mode == "paste":
    # like the real input component: a chunk that ends with CR/LF is a paste — the
    # newline inside it does not submit; only a *separate* Enter does.
    import os, select
    chunk = os.read(0, 4096).decode()
    code = chunk.strip()
    while True:
        r, _, _ = select.select([0], [], [], 25)
        if not r:
            sys.exit(3)
        nxt = os.read(0, 4096).decode()
        if "\\r" in nxt or "\\n" in nxt:
            code = (code + nxt.replace("\\r", "").replace("\\n", "")).strip()
            break
        code += nxt
else:
    code = sys.stdin.readline().strip()
if code == "good-code":
    print("")
    print("{FAKE_TOKEN}")
    sys.exit(0)
print("")
print("OAuth error: Invalid code. Please make sure the full code was copied Press Enter to retry.")
sys.stdin.readline()
sys.exit(1)
'''


async def _ok(token: str):
    return True, None


async def _bad(token: str):
    return False, "401 OAuth access token is invalid"


@pytest.fixture
def fake_cli(tmp_path: Path):
    def make(mode: str = "normal") -> str:
        path = tmp_path / f"claude-{mode}"
        path.write_text(FAKE_CLI.replace("{mode}", mode))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    return make


async def _outcome(flows, auth_id, timeout=10.0):
    """Poll like the plugin does: status until result_available, then claim it."""
    for _ in range(int(timeout * 20)):
        st = flows.status()
        if st.get("auth_id") == auth_id and st.get("result_available"):
            return await flows.result(auth_id)
        await asyncio.sleep(0.05)
    raise AssertionError("no outcome")


async def test_start_submit_bad_then_good(fake_cli):
    from bridge.auth import AuthFlowNotReady

    flows = AuthFlowManager(fake_cli(), url_timeout=10, submit_timeout=10, verifier=_ok)
    started = await flows.start()
    assert started["url"].startswith("https://claude.com/cai/oauth/authorize?code=true")
    assert started["expires_in"] == 600 and started["auth_id"]
    status = flows.status()
    assert status["pending"] is True and status["auth_id"] == started["auth_id"] and status["url"] == started["url"]
    assert status["phase"] == "awaiting_code" and status["result_available"] is False

    with pytest.raises(AuthFlowPending):
        await flows.start()

    accepted = await flows.submit(started["auth_id"], "nope")
    assert accepted == {"accepted": True, "auth_id": started["auth_id"]}
    assert flows.status()["phase"] == "exchanging"
    with pytest.raises(AuthFlowError):           # a second code while exchanging
        await flows.submit(started["auth_id"], "again")
    outcome = await _outcome(flows, started["auth_id"])
    assert outcome["ok"] is False and "Invalid code" in outcome["error"]
    assert flows.pending is None                 # claiming the outcome ends the flow

    started = await flows.start()
    await flows.submit(started["auth_id"], "good-code")
    with pytest.raises((AuthFlowNotReady, AssertionError)):
        # not finished yet (or already finished on a fast machine — then _outcome below works)
        if not flows.status().get("result_available"):
            await flows.result(started["auth_id"])
        raise AssertionError
    outcome = await _outcome(flows, started["auth_id"])
    assert outcome == {"ok": True, "token": FAKE_TOKEN}
    assert flows.pending is None and flows.status()["pending"] is False
    # the token is handed out exactly once
    from bridge.auth import AuthFlowNotFound

    with pytest.raises(AuthFlowNotFound):
        await flows.result(started["auth_id"])


async def test_failed_outcome_is_visible_in_status_until_claimed(fake_cli):
    flows = AuthFlowManager(fake_cli(), url_timeout=10, submit_timeout=10, verifier=_ok)
    started = await flows.start()
    await flows.submit(started["auth_id"], "nope")
    for _ in range(200):
        if flows.status().get("result_available"):
            break
        await asyncio.sleep(0.05)
    st = flows.status()
    assert st["phase"] == "failed" and "Invalid code" in st["error"] and st["pending"] is True
    assert flows.pending.proc.returncode is not None   # CLI stopped, outcome kept
    assert (await flows.result(started["auth_id"]))["ok"] is False
    assert flows.pending is None


async def test_cancel_and_unknown_ids(fake_cli):
    flows = AuthFlowManager(fake_cli(), url_timeout=10)
    started = await flows.start()
    assert await flows.cancel("other") is False
    assert await flows.cancel(started["auth_id"]) is True
    assert flows.pending is None
    from bridge.auth import AuthFlowNotFound

    with pytest.raises(AuthFlowNotFound):
        await flows.submit(started["auth_id"], "x")


async def test_no_url_is_an_error(fake_cli):
    flows = AuthFlowManager(fake_cli("silent"), url_timeout=3)
    with pytest.raises(AuthFlowError):
        await flows.start()
    assert flows.pending is None


async def test_missing_cli():
    flows = AuthFlowManager(None)
    with pytest.raises(AuthFlowError):
        await flows.start()


def test_scrub_hides_tokens():
    assert "sk-ant-***" in scrub(f"token {FAKE_TOKEN} end") and FAKE_TOKEN not in scrub(FAKE_TOKEN)


async def test_expiry_terminates_flow(fake_cli):
    flows = AuthFlowManager(fake_cli(), url_timeout=10, expires_in=1)
    await flows.start()
    proc = flows.pending.proc
    await asyncio.sleep(1.3)
    assert flows.pending is None and flows.status()["pending"] is False
    assert proc.returncode is not None  # the CLI process was terminated, not leaked


def test_clean_url_cuts_glued_hint_text():
    from bridge.auth import clean_url

    glued = ("https://claude.com/cai/oauth/authorize?code=true&client_id=abc&code_challenge=x&code_challenge_method=S256"
             "&state=YTcDvX8-OnbuTmG06pIQm2yklvxuOTtW33Opo6klaPkHoldShiftwhileselectingtouseyourterminal")
    assert clean_url(glued).endswith("&state=YTcDvX8-OnbuTmG06pIQm2yklvxuOTtW33Opo6klaPk")
    assert clean_url("https://claude.com/x?state=abcPastecodehereifprompted>") == "https://claude.com/x?state=abc"
    assert clean_url("https://claude.com/x?state=abc") == "https://claude.com/x?state=abc"


async def test_enter_is_sent_separately_for_paste_detection(fake_cli):
    flows = AuthFlowManager(fake_cli("paste"), url_timeout=10, submit_timeout=10, enter_delay=0.2, nudge_after=2, verifier=_ok)
    started = await flows.start()
    await flows.submit(started["auth_id"], "good-code")
    outcome = await _outcome(flows, started["auth_id"], timeout=12)
    assert outcome == {"ok": True, "token": FAKE_TOKEN}


async def test_token_that_fails_validation_is_not_handed_out(fake_cli):
    flows = AuthFlowManager(fake_cli(), url_timeout=10, submit_timeout=10, verifier=_bad)
    started = await flows.start()
    await flows.submit(started["auth_id"], "good-code")
    outcome = await _outcome(flows, started["auth_id"])
    assert outcome["ok"] is False and "did not validate" in outcome["error"] and "401" in outcome["error"]
    assert FAKE_TOKEN not in outcome["error"]          # scrubbed


def test_strip_ansi_turns_cursor_moves_into_line_breaks():
    from bridge.auth import TOKEN_RE, strip_ansi

    # ink draws the next hint with a cursor-down + column move instead of "\n"
    raw = (b"Your token:\x1b[1B\x1b[1G" + FAKE_TOKEN.encode() + b"\x1b[1B\x1b[1GStore this token securely.\x1b[0m")
    text = strip_ansi(raw)
    assert TOKEN_RE.search(text).group(0) == FAKE_TOKEN
    assert "Store this token" in text and FAKE_TOKEN + "Store" not in text
