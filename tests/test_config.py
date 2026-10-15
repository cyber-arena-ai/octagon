"""Focused tests for per-challenge game.yaml parsing."""

from __future__ import annotations

import pytest

from gamemgr.config import ChallengeConfig, _challenge_from_dict

_BASE = {"name": "notes", "type": "notes", "port": 5000, "deploy_on": ["team1"]}


def test_challenge_without_interval_defaults_none():
    cfg = _challenge_from_dict(0, dict(_BASE))
    assert isinstance(cfg, ChallengeConfig)
    assert cfg.health_interval_secs is None


def test_challenge_with_valid_interval_is_float():
    cfg = _challenge_from_dict(0, {**_BASE, "health_interval_secs": 30})
    assert cfg.health_interval_secs == 30.0
    assert isinstance(cfg.health_interval_secs, float)


@pytest.mark.parametrize("bad", [0, -5, -0.1, True, "30", [], {}])
def test_challenge_with_bad_interval_raises_at_load(bad):
    with pytest.raises(ValueError, match="health_interval_secs must be a positive"):
        _challenge_from_dict(2, {**_BASE, "name": "mlflow-lfi", "health_interval_secs": bad})


def test_bad_interval_error_names_the_entry():
    with pytest.raises(ValueError, match=r"challenges\[2\] 'mlflow-lfi'"):
        _challenge_from_dict(2, {**_BASE, "name": "mlflow-lfi", "health_interval_secs": -1})


def test_challenge_level_defaults_to_easy():
    assert _challenge_from_dict(0, dict(_BASE)).level == "easy"


@pytest.mark.parametrize("level", ["easy", "hard"])
def test_challenge_level_preserves_supported_value(level):
    assert _challenge_from_dict(0, {**_BASE, "level": level}).level == level


@pytest.mark.parametrize("level", ["medium", None])
def test_challenge_level_rejects_unsupported_value(level):
    with pytest.raises(ValueError) as exc_info:
        _challenge_from_dict(2, {**_BASE, "name": "mlflow-lfi", "level": level})
    assert "challenges[2] 'mlflow-lfi'" in str(exc_info.value)
