"""Additional coverage for :mod:`app.config`.

Complements tests/test_security_config.py: the remaining field and
model validators, the local-environment / debug-endpoint properties,
CORS origin parsing, the AIBackend enum, and settings caching.
"""

import unittest

from app.config import AIBackend, Settings, get_settings


def _valid_kwargs(**overrides):
    """Settings kwargs that always pass the JWT secret validator."""
    kwargs = {"jwt_secret_key": "a" * 32}
    kwargs.update(overrides)
    return kwargs


class CredentialTtlValidatorTests(unittest.TestCase):
    def test_rejects_ttl_below_sixty(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(credential_cache_ttl_seconds=59))

    def test_accepts_ttl_of_sixty(self):
        settings = Settings(**_valid_kwargs(credential_cache_ttl_seconds=60))
        self.assertEqual(settings.credential_cache_ttl_seconds, 60)


class StopLossIntervalValidatorTests(unittest.TestCase):
    def test_rejects_zero_interval(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(stop_loss_check_interval_seconds=0))

    def test_accepts_interval_of_one(self):
        settings = Settings(**_valid_kwargs(stop_loss_check_interval_seconds=1))
        self.assertEqual(settings.stop_loss_check_interval_seconds, 1)


class SlippageBoundsValidatorTests(unittest.TestCase):
    def test_rejects_negative_min(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(slippage_sim_min=-0.001))

    def test_rejects_max_below_min(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(slippage_sim_min=0.02, slippage_sim_max=0.01))

    def test_accepts_valid_bounds(self):
        settings = Settings(**_valid_kwargs(slippage_sim_min=0.003, slippage_sim_max=0.03))
        self.assertEqual(settings.slippage_sim_min, 0.003)
        self.assertEqual(settings.slippage_sim_max, 0.03)


class LatencyArbValidatorTests(unittest.TestCase):
    def test_rejects_edge_threshold_out_of_range(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_edge_threshold=0.0))
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_edge_threshold=1.0))

    def test_rejects_non_positive_max_notional(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_max_notional=0.0))

    def test_rejects_invalid_latency_budget(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_max_latency_ms=0))

    def test_rejects_invalid_cycle_seconds(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_cycle_seconds=0))

    def test_rejects_negative_daily_loss_limit(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_daily_loss_limit=-1.0))

    def test_rejects_invalid_circuit_breaker_losses(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_circuit_breaker_losses=0))

    def test_rejects_invalid_circuit_breaker_resume(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_circuit_breaker_resume_seconds=0))

    def test_rejects_invalid_late_entry_seconds(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_late_entry_seconds=0))

    def test_rejects_negative_min_depth(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_min_depth=-1.0))

    def test_rejects_max_spread_out_of_range(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_max_spread=0.0))
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(latency_arb_max_spread=1.0))

    def test_accepts_default_latency_arb_settings(self):
        settings = Settings(**_valid_kwargs())
        self.assertEqual(settings.latency_arb_edge_threshold, 0.03)
        self.assertEqual(settings.latency_arb_max_notional, 50.0)


class PositiveIntValidatorTests(unittest.TestCase):
    def test_rejects_zero_trade_poll_concurrency(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(trade_poll_max_concurrency=0))

    def test_rejects_zero_http_max_connections(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(trade_poll_http_max_connections=0))

    def test_rejects_zero_keepalive_connections(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(trade_poll_http_keepalive_connections=0))

    def test_rejects_zero_proxy_cache_ttl(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(proxy_cache_ttl_seconds=0))

    def test_rejects_zero_proxy_cache_entries(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(proxy_cache_max_entries=0))

    def test_rejects_zero_memory_cache_entries(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(in_memory_cache_max_entries=0))

    def test_rejects_zero_sse_clients_per_user(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(price_stream_max_sse_clients_per_user=0))

    def test_rejects_zero_price_stream_subscriptions(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(price_stream_max_subscriptions=0))

    def test_rejects_zero_gtc_ttl(self):
        with self.assertRaises(ValueError):
            Settings(**_valid_kwargs(gtc_ttl_seconds=0))

    def test_accepts_valid_positive_ints(self):
        settings = Settings(**_valid_kwargs(trade_poll_max_concurrency=10))
        self.assertEqual(settings.trade_poll_max_concurrency, 10)


class SettingsPropertyTests(unittest.TestCase):
    def test_is_local_environment_accepts_local_aliases(self):
        for env in ("dev", "development", "local", "test"):
            settings = Settings(**_valid_kwargs(environment=env))
            self.assertTrue(settings.is_local_environment, env)

    def test_is_local_environment_rejects_production(self):
        settings = Settings(**_valid_kwargs(environment="production"))
        self.assertFalse(settings.is_local_environment)

    def test_debug_endpoints_active_when_explicitly_enabled(self):
        settings = Settings(
            **_valid_kwargs(debug_endpoints_enabled=True, debug_log_hash_salt="salt")
        )
        self.assertTrue(settings.debug_endpoints_active)

    def test_debug_endpoints_active_in_local_environment(self):
        settings = Settings(**_valid_kwargs(environment="test"))
        self.assertTrue(settings.debug_endpoints_active)

    def test_debug_endpoints_inactive_in_production(self):
        settings = Settings(**_valid_kwargs(environment="production"))
        self.assertFalse(settings.debug_endpoints_active)

    def test_cors_allowed_origins_list_parses_and_strips(self):
        settings = Settings(**_valid_kwargs(cors_allowed_origins=" http://a , , http://b ,, "))
        self.assertEqual(settings.cors_allowed_origins_list, ["http://a", "http://b"])

    def test_cors_allowed_origins_list_empty(self):
        settings = Settings(**_valid_kwargs(cors_allowed_origins=""))
        self.assertEqual(settings.cors_allowed_origins_list, [])


class AIBackendTests(unittest.TestCase):
    def test_backend_values(self):
        self.assertEqual(AIBackend.LLM_CHAIN, "llm_chain")
        self.assertEqual(AIBackend.CLI_AGENT, "cli_agent")


class GetSettingsTests(unittest.TestCase):
    def test_get_settings_is_cached(self):
        self.assertIs(get_settings(), get_settings())


if __name__ == "__main__":
    unittest.main()
