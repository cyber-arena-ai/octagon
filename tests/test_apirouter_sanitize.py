"""apirouter.sanitize — markdown-fence repair for upstream JSON bodies."""

import json

from apirouter.sanitize import unwrap_markdown_json

CLEAN = json.dumps({"id": "x", "choices": [{"message": {"content": "hi"}}]})


def test_clean_json_untouched():
    assert unwrap_markdown_json(CLEAN) == CLEAN


def test_strips_json_fence():
    wrapped = f"```json\n{CLEAN}\n```"
    assert unwrap_markdown_json(wrapped) == CLEAN


def test_strips_bare_fence():
    wrapped = f"```\n{CLEAN}\n```"
    assert unwrap_markdown_json(wrapped) == CLEAN


def test_strips_fence_after_prose():
    wrapped = f"Here is the response:\n```json\n{CLEAN}\n```"
    assert unwrap_markdown_json(wrapped) == CLEAN


def test_idempotent():
    once = unwrap_markdown_json(f"```json\n{CLEAN}\n```")
    assert unwrap_markdown_json(once) == once == CLEAN


def test_non_json_fence_left_alone():
    # A fenced python block is not JSON — must not be unwrapped (would corrupt).
    code = "```python\nprint('hi')\n```"
    assert unwrap_markdown_json(code) == code


def test_json_array_body():
    arr = json.dumps([1, 2, 3])
    assert unwrap_markdown_json(f"```json\n{arr}\n```") == arr


def test_empty_and_plain_text():
    assert unwrap_markdown_json("") == ""
    assert unwrap_markdown_json("not json at all") == "not json at all"


def test_trailing_whitespace_tolerated():
    wrapped = f"```json\n{CLEAN}\n```   \n"
    assert unwrap_markdown_json(wrapped) == CLEAN
