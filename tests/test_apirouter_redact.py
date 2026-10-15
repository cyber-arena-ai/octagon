"""apirouter.metering.redact / token_fingerprint — pure secret-scrubbing helpers.

These gate every error-body / exception string before it enters a log line or a
usage.jsonl row, so they are unit-tested directly (the gateway's end-to-end
secret-safety is pinned separately in test_apirouter_debuggability).
"""

from apirouter.metering import redact, token_fingerprint


def test_empty_and_clean_pass_through():
    assert redact("") == ""
    # A real 404 error body must survive intact — the operator needs to read it.
    body = ('{"error":{"message":"model gpt-9 does not exist",'
            '"type":"invalid_request_error","code":"model_not_found"}}')
    assert redact(body) == body


def test_redacts_anthropic_key_echoed_in_body():
    body = 'invalid x-api-key: sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF please check'
    out = redact(body)
    assert "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF" not in out
    assert "<redacted>" in out
    assert "please check" in out  # surrounding context is preserved


def test_redacts_openai_style_key():
    out = redact('{"error":"Incorrect API key provided: sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX"}')
    assert "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX" not in out and "<redacted>" in out


def test_redacts_google_and_bearer_and_principal_shapes():
    assert "AIzaSyA1234567890abcdefghijklmnopqrstuv" not in redact(
        "key=AIzaSyA1234567890abcdefghijklmnopqrstuv")
    assert "sekret" not in redact("Authorization: Bearer abcdefsekret1234567890")
    # A router principal token shape (cad-<team>) must never survive either.
    assert "cad-team7-9f2a1b" not in redact("presented cad-team7-9f2a1b")


def test_exact_strip_of_known_secret_even_when_not_key_shaped():
    # The injected upstream key may not match any generic shape; passing it as a
    # known secret strips it verbatim.
    key = "REAL-UPSTREAM-KEY-abcdef123456"
    out = redact(f"upstream said: {key} rejected", known_secrets=(key,))
    assert key not in out and "<redacted>" in out


def test_truncates_after_redaction_so_key_cannot_survive_at_boundary():
    # Redaction runs BEFORE truncation: the key at the front is scrubbed, then the
    # tail filler is cut — the raw key never appears regardless of the cap. (Filler
    # is broken by spaces/periods so the long-blob catch-all doesn't swallow it.)
    text = "sk-ant-" + "A" * 40 + " tail." * 200
    out = redact(text, limit=200)
    assert "sk-ant-AAAA" not in out
    assert len(out) <= 200 + len("…(truncated)")
    assert out.endswith("…(truncated)")


def test_catch_all_long_blob():
    blob = "Zk9" + "aB3cD5eF7" * 6  # 57 chars of base64url-ish → looks like a secret
    assert blob not in redact(f"token={blob}")


def test_token_fingerprint_never_reveals_token():
    assert token_fingerprint(None) == "missing"
    assert token_fingerprint("") == "missing"
    fp = token_fingerprint("cad-team1-secret")
    assert "cad-team1-secret" not in fp and fp.startswith("sha256:")
    # deterministic + discriminating
    assert token_fingerprint("cad-team1-secret") == fp
    assert token_fingerprint("cad-team2-secret") != fp
