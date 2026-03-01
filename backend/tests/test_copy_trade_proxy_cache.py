import unittest
from unittest.mock import patch

import app.services.copy_trade_service as copy_trade_service


class CopyTradeProxyCacheTests(unittest.TestCase):
    def setUp(self):
        copy_trade_service._proxy_address_cache.clear()

    def tearDown(self):
        copy_trade_service._proxy_address_cache.clear()

    def test_proxy_cache_applies_ttl_expiration(self):
        with patch.object(copy_trade_service, "_PROXY_CACHE_TTL_SECONDS", 1), patch.object(
            copy_trade_service, "_PROXY_CACHE_MAX_ENTRIES", 10
        ):
            copy_trade_service._set_cached_proxy("0xabc", "0xproxy", now_ts=10.0)
            self.assertEqual(
                copy_trade_service._get_cached_proxy("0xabc", now_ts=10.5),
                "0xproxy",
            )
            self.assertIsNone(
                copy_trade_service._get_cached_proxy("0xabc", now_ts=12.0),
            )

    def test_proxy_cache_enforces_max_entries_with_lru_eviction(self):
        with patch.object(copy_trade_service, "_PROXY_CACHE_TTL_SECONDS", 9999), patch.object(
            copy_trade_service, "_PROXY_CACHE_MAX_ENTRIES", 2
        ):
            copy_trade_service._set_cached_proxy("0x1", "0xp1", now_ts=1.0)
            copy_trade_service._set_cached_proxy("0x2", "0xp2", now_ts=1.0)
            self.assertEqual(copy_trade_service._get_cached_proxy("0x1", now_ts=1.1), "0xp1")

            # Inserting a 3rd entry should evict 0x2 (least recently used).
            copy_trade_service._set_cached_proxy("0x3", "0xp3", now_ts=1.2)

            self.assertIsNone(copy_trade_service._get_cached_proxy("0x2", now_ts=1.3))
            self.assertEqual(copy_trade_service._get_cached_proxy("0x1", now_ts=1.3), "0xp1")
            self.assertEqual(copy_trade_service._get_cached_proxy("0x3", now_ts=1.3), "0xp3")


if __name__ == "__main__":
    unittest.main()
