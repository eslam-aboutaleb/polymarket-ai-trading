import unittest

import pytest

from app.config import Settings


@pytest.mark.parametrize("enabled", [False, True])
def test_environment_can_enable_and_disable_live_features(monkeypatch, enabled):
    flags = (
        "live_trading_enabled",
        "live_market_making_enabled",
        "live_copy_trading_enabled",
        "live_inverse_trading_enabled",
        "live_latency_arb_enabled",
        "latency_arb_live",
    )
    for name in flags:
        monkeypatch.setenv(name.upper(), str(enabled).lower())
    settings = Settings(_env_file=None, jwt_secret_key="a" * 32, environment="test")
    assert all(getattr(settings, name) is enabled for name in flags)


def test_live_features_default_enabled_and_allow_explicit_operator_disable(monkeypatch):
    flags = (
        "live_trading_enabled",
        "live_market_making_enabled",
        "live_copy_trading_enabled",
        "live_inverse_trading_enabled",
        "live_latency_arb_enabled",
        "latency_arb_live",
    )
    for name in flags:
        monkeypatch.delenv(name.upper(), raising=False)
    config = {"_env_file": None, "jwt_secret_key": "a" * 32, "environment": "test"}
    defaults = Settings(**config)
    assert all(getattr(defaults, name) is True for name in flags)
    disabled = Settings(**config, **dict.fromkeys(flags, False))
    assert all(getattr(disabled, name) is False for name in flags)


class SettingsValidationTests(unittest.TestCase):
    def test_jwt_secret_rejects_placeholder(self):
        with self.assertRaises(ValueError):
            Settings(jwt_secret_key="CHANGE-ME-IN-PRODUCTION")

    def test_jwt_secret_rejects_short_value(self):
        with self.assertRaises(ValueError):
            Settings(jwt_secret_key="short-secret")

    def test_debug_salt_required_when_debug_enabled(self):
        with self.assertRaises(ValueError):
            Settings(
                jwt_secret_key="a" * 32,
                debug_endpoints_enabled=True,
                debug_log_hash_salt="",
            )

    def test_valid_security_configuration(self):
        settings = Settings(
            jwt_secret_key="a" * 32,
            debug_endpoints_enabled=True,
            debug_log_hash_salt="salt",
            credential_cache_ttl_seconds=3600,
        )
        self.assertEqual(settings.jwt_secret_key, "a" * 32)


if __name__ == "__main__":
    unittest.main()
