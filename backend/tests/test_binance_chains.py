"""Tests for the canonical Binance chain map shared by all integrations.

The Solana value is pinned on purpose: the research module and the backend
route used to disagree (``CT_501`` vs ``501``), and only ``CT_501`` returns
data from Binance. See ``services/shared/binance_chains/__init__.py`` for the
live-curl evidence.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "shared"))

from binance_chains import (  # noqa: E402
    DEFAULT_CHAIN_ID,
    SOLANA_CHAIN_ID,
    SUPPORTED_CHAINS,
    chain_to_platform,
)


class SupportedChainsTests(unittest.TestCase):
    """The map itself, with the verified Solana id pinned."""

    def test_solana_is_the_verified_ct_501(self):
        self.assertEqual(SUPPORTED_CHAINS["solana"], "CT_501")
        self.assertEqual(SOLANA_CHAIN_ID, "CT_501")

    def test_route_pinned_chain_ids_are_unchanged(self):
        # The dashboard route tests pin these values.
        expected = {
            "ethereum": "1",
            "polygon": "137",
            "bsc": "56",
            "arbitrum": "42161",
            "base": "8453",
        }
        for chain, chain_id in expected.items():
            self.assertEqual(SUPPORTED_CHAINS[chain], chain_id)

    def test_unknown_chain_falls_back_to_ethereum(self):
        self.assertEqual(DEFAULT_CHAIN_ID, "1")

    def test_map_has_exactly_the_documented_chains(self):
        self.assertEqual(
            set(SUPPORTED_CHAINS),
            {"ethereum", "polygon", "bsc", "arbitrum", "base", "solana"},
        )


class ChainToPlatformTests(unittest.TestCase):
    """Name normalisation and fallback behaviour."""

    def test_exact_name(self):
        self.assertEqual(chain_to_platform("solana"), "CT_501")

    def test_case_insensitive(self):
        self.assertEqual(chain_to_platform("SoLaNa"), "CT_501")
        self.assertEqual(chain_to_platform("BSC"), "56")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(chain_to_platform("  base  "), "8453")

    def test_unknown_name_uses_default(self):
        self.assertEqual(chain_to_platform("dogchain"), DEFAULT_CHAIN_ID)

    def test_empty_and_none_use_default(self):
        self.assertEqual(chain_to_platform(""), DEFAULT_CHAIN_ID)
        self.assertEqual(chain_to_platform(None), DEFAULT_CHAIN_ID)

    def test_explicit_default_is_honoured(self):
        # The research module keeps its Solana default for unknown chains.
        self.assertEqual(chain_to_platform("dogchain", default=SOLANA_CHAIN_ID), "CT_501")


if __name__ == "__main__":
    unittest.main()
