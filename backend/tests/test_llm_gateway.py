"""Unit tests for the LLM provider gateway (``app.services.llm_gateway``).

The gateway is a pure factory: no network, no gRPC, no database. Everything it
decides is observable from the ``(AIBackend, LLMConfig)`` tuples it returns and
from the provider catalogue it advertises, so these tests assert exact values and
exact call arguments rather than merely exercising the code paths.

Covered:

* ``LLMConfig.to_proto_dict`` -- which fields reach the wire and which are
  dropped (zero/negative temperature and token caps are omitted on purpose).
* the provider -> proto-enum and provider -> backend lookup tables, including
  the "unknown provider" fallbacks that default to proto 0 / ``LLM_CHAIN``.
* ``LLMGateway.get_backend_for_provider`` -- backend routing and config echoing.
* ``LLMGateway.resolve_provider`` -- the full three-level precedence chain
  (request override, user preference, legacy ``ai_backend``, system default)
  plus every invalid-input fall-through between those levels.
* ``list_providers`` / ``get_default_model`` -- the UI-facing catalogue.
* ``get_llm_gateway`` -- singleton creation and reuse.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import AIBackend
from app.models.user_settings import AIBackendType
from app.services import llm_gateway
from app.services.llm_gateway import (
    MODEL_SUGGESTIONS,
    PROVIDER_TO_BACKEND,
    PROVIDER_TO_PROTO,
    LLMConfig,
    LLMGateway,
    LLMProviderType,
    get_llm_gateway,
)


def _gateway(default_ai_backend=AIBackend.CLI_AGENT) -> LLMGateway:
    """Build a gateway whose settings object is a controlled stub."""
    settings = SimpleNamespace(default_ai_backend=default_ai_backend)
    with patch.object(llm_gateway, "get_settings", return_value=settings):
        return LLMGateway()


class ProviderMappingTests(unittest.TestCase):
    """The static lookup tables that translate enum -> proto / backend."""

    def test_every_provider_has_a_proto_value(self):
        self.assertEqual(set(PROVIDER_TO_PROTO), set(LLMProviderType))

    def test_every_provider_has_a_backend(self):
        self.assertEqual(set(PROVIDER_TO_BACKEND), set(LLMProviderType))

    def test_proto_values_match_the_analysis_proto_enum(self):
        self.assertEqual(
            PROVIDER_TO_PROTO,
            {
                LLMProviderType.OPENAI: 1,
                LLMProviderType.ANTHROPIC: 2,
                LLMProviderType.GOOGLE: 3,
                LLMProviderType.GROQ: 4,
                LLMProviderType.OLLAMA: 5,
                LLMProviderType.GITHUB_MODELS: 6,
            },
        )

    def test_only_github_models_is_routed_to_the_cli_agent(self):
        routed = {
            provider.value: backend.value
            for provider, backend in PROVIDER_TO_BACKEND.items()
            if backend is AIBackend.CLI_AGENT
        }
        self.assertEqual(routed, {"github_models": "cli_agent"})

    def test_provider_values_are_the_lowercase_wire_strings(self):
        self.assertEqual(
            sorted(p.value for p in LLMProviderType),
            ["anthropic", "github_models", "google", "groq", "ollama", "openai"],
        )


class LLMConfigToProtoDictTests(unittest.TestCase):
    """Serialisation of a per-request LLM configuration."""

    def test_empty_config_serialises_to_nothing(self):
        self.assertEqual(LLMConfig().to_proto_dict(), {})

    def test_provider_only(self):
        self.assertEqual(
            LLMConfig(provider=LLMProviderType.ANTHROPIC).to_proto_dict(),
            {"provider": 2},
        )

    def test_model_only(self):
        self.assertEqual(LLMConfig(model="gpt-4o").to_proto_dict(), {"model": "gpt-4o"})

    def test_zero_temperature_is_omitted(self):
        self.assertNotIn("temperature", LLMConfig(temperature=0.0).to_proto_dict())

    def test_negative_temperature_is_omitted(self):
        self.assertNotIn("temperature", LLMConfig(temperature=-1.0).to_proto_dict())

    def test_positive_temperature_is_kept(self):
        self.assertEqual(LLMConfig(temperature=0.3).to_proto_dict(), {"temperature": 0.3})

    def test_zero_max_tokens_is_omitted(self):
        self.assertNotIn("max_tokens", LLMConfig(max_tokens=0).to_proto_dict())

    def test_positive_max_tokens_is_kept(self):
        self.assertEqual(LLMConfig(max_tokens=2048).to_proto_dict(), {"max_tokens": 2048})

    def test_unmapped_provider_falls_back_to_proto_zero(self):
        self.assertEqual(LLMConfig(provider="mystery-llm").to_proto_dict(), {"provider": 0})

    def test_all_fields_together(self):
        config = LLMConfig(
            provider=LLMProviderType.GROQ,
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=512,
        )
        self.assertEqual(
            config.to_proto_dict(),
            {
                "provider": 4,
                "model": "llama-3.3-70b-versatile",
                "temperature": 0.7,
                "max_tokens": 512,
            },
        )


class GetBackendForProviderTests(unittest.TestCase):
    """Backend routing plus verbatim echo of the per-request overrides."""

    def setUp(self):
        self.gateway = _gateway()

    def test_ollama_uses_the_llm_chain_backend(self):
        backend, config = self.gateway.get_backend_for_provider(LLMProviderType.OLLAMA)
        self.assertIs(backend, AIBackend.LLM_CHAIN)
        self.assertIs(config.provider, LLMProviderType.OLLAMA)

    def test_github_models_uses_the_cli_agent_backend(self):
        backend, config = self.gateway.get_backend_for_provider(LLMProviderType.GITHUB_MODELS)
        self.assertIs(backend, AIBackend.CLI_AGENT)
        self.assertIs(config.provider, LLMProviderType.GITHUB_MODELS)

    def test_unmapped_provider_defaults_to_llm_chain(self):
        backend, config = self.gateway.get_backend_for_provider("not-a-provider")
        self.assertIs(backend, AIBackend.LLM_CHAIN)
        self.assertEqual(config.provider, "not-a-provider")

    def test_overrides_are_carried_into_the_config(self):
        _, config = self.gateway.get_backend_for_provider(
            LLMProviderType.OPENAI, model="gpt-4o", temperature=0.2, max_tokens=1024
        )
        self.assertEqual(config.model, "gpt-4o")
        self.assertEqual(config.temperature, 0.2)
        self.assertEqual(config.max_tokens, 1024)

    def test_omitted_overrides_stay_none(self):
        _, config = self.gateway.get_backend_for_provider(LLMProviderType.GOOGLE)
        self.assertIsNone(config.model)
        self.assertIsNone(config.temperature)
        self.assertIsNone(config.max_tokens)

    def test_config_round_trips_into_the_proto_dict(self):
        _, config = self.gateway.get_backend_for_provider(
            LLMProviderType.ANTHROPIC, model="claude-sonnet-4"
        )
        self.assertEqual(config.to_proto_dict(), {"provider": 2, "model": "claude-sonnet-4"})


class ResolveProviderTests(unittest.TestCase):
    """Three-level provider precedence and the fall-throughs between levels."""

    def setUp(self):
        self.gateway = _gateway(default_ai_backend=AIBackend.CLI_AGENT)

    # ── priority 1: request override ───────────────────────────────────

    def test_request_provider_wins(self):
        backend, config = self.gateway.resolve_provider(request_provider="anthropic")
        self.assertIs(backend, AIBackend.LLM_CHAIN)
        self.assertIs(config.provider, LLMProviderType.ANTHROPIC)

    def test_request_provider_is_case_insensitive(self):
        _, config = self.gateway.resolve_provider(request_provider="GitHub_Models")
        self.assertIs(config.provider, LLMProviderType.GITHUB_MODELS)
        self.assertIsInstance(config, LLMConfig)

    def test_request_provider_routing_reaches_the_cli_agent(self):
        backend, _ = self.gateway.resolve_provider(request_provider="github_models")
        self.assertIs(backend, AIBackend.CLI_AGENT)

    def test_request_model_is_forwarded(self):
        _, config = self.gateway.resolve_provider(
            request_provider="openai", request_model="gpt-4o-mini"
        )
        self.assertEqual(config.model, "gpt-4o-mini")

    def test_request_provider_beats_user_preference(self):
        user = SimpleNamespace(preferred_llm_provider="ollama", preferred_llm_model="llama3.2")
        _, config = self.gateway.resolve_provider(request_provider="openai", user_settings=user)
        self.assertIs(config.provider, LLMProviderType.OPENAI)

    def test_invalid_request_provider_falls_through_to_user_preference(self):
        user = SimpleNamespace(preferred_llm_provider="groq", preferred_llm_model="mixtral-8x7b")
        _, config = self.gateway.resolve_provider(request_provider="gpt9000", user_settings=user)
        self.assertIs(config.provider, LLMProviderType.GROQ)

    def test_invalid_request_provider_without_user_settings_uses_the_default(self):
        backend, config = self.gateway.resolve_provider(request_provider="nope")
        self.assertIs(backend, AIBackend.CLI_AGENT)
        self.assertIsNone(config)

    # ── priority 2: user preference ────────────────────────────────────

    def test_user_preference_is_used_when_no_request_override(self):
        user = SimpleNamespace(preferred_llm_provider="OPENAI", preferred_llm_model="gpt-4o")
        backend, config = self.gateway.resolve_provider(user_settings=user)
        self.assertIs(backend, AIBackend.LLM_CHAIN)
        self.assertIs(config.provider, LLMProviderType.OPENAI)
        self.assertEqual(config.model, "gpt-4o")

    def test_user_preference_without_a_model_leaves_the_model_unset(self):
        user = SimpleNamespace(preferred_llm_provider="google")
        _, config = self.gateway.resolve_provider(user_settings=user)
        self.assertIsNone(config.model)

    def test_invalid_user_preference_falls_through_to_the_default(self):
        user = SimpleNamespace(preferred_llm_provider="skynet")
        backend, config = self.gateway.resolve_provider(user_settings=user)
        self.assertIs(backend, AIBackend.CLI_AGENT)
        self.assertIsNone(config)

    def test_invalid_user_preference_falls_through_to_the_legacy_field(self):
        user = SimpleNamespace(
            preferred_llm_provider="skynet", ai_backend=AIBackendType.CLI_AGENT.value
        )
        backend, config = self.gateway.resolve_provider(user_settings=user)
        self.assertIs(backend, AIBackend.CLI_AGENT)
        self.assertIsNone(config)

    def test_empty_user_settings_record_falls_through_to_the_default(self):
        backend, config = self.gateway.resolve_provider(user_settings=SimpleNamespace())
        self.assertIs(backend, AIBackend.CLI_AGENT)
        self.assertIsNone(config)

    def test_blank_user_preference_falls_through_to_the_legacy_field(self):
        user = SimpleNamespace(preferred_llm_provider="", ai_backend=AIBackendType.LLM_CHAIN.value)
        backend, config = self.gateway.resolve_provider(user_settings=user)
        self.assertIs(backend, AIBackend.LLM_CHAIN)
        self.assertIsNone(config)

    # ── legacy ai_backend field ────────────────────────────────────────

    def test_legacy_cli_agent_setting_maps_to_the_cli_agent(self):
        user = SimpleNamespace(ai_backend=AIBackendType.CLI_AGENT.value)
        backend, config = self.gateway.resolve_provider(user_settings=user)
        self.assertIs(backend, AIBackend.CLI_AGENT)
        self.assertIsNone(config)

    def test_legacy_llm_chain_setting_maps_to_llm_chain(self):
        user = SimpleNamespace(ai_backend=AIBackendType.LLM_CHAIN.value)
        backend, config = self.gateway.resolve_provider(user_settings=user)
        self.assertIs(backend, AIBackend.LLM_CHAIN)
        self.assertIsNone(config)

    def test_unknown_legacy_backend_value_falls_through_to_the_default(self):
        user = SimpleNamespace(ai_backend="some_retired_backend")
        backend, config = self.gateway.resolve_provider(user_settings=user)
        self.assertIs(backend, AIBackend.CLI_AGENT)
        self.assertIsNone(config)

    # ── priority 3: system default ─────────────────────────────────────

    def test_explicit_default_backend_beats_the_settings_default(self):
        backend, config = self.gateway.resolve_provider(default_backend=AIBackend.LLM_CHAIN)
        self.assertIs(backend, AIBackend.LLM_CHAIN)
        self.assertIsNone(config)

    def test_settings_default_is_used_when_nothing_else_is_specified(self):
        backend, config = self.gateway.resolve_provider()
        self.assertIs(backend, AIBackend.CLI_AGENT)
        self.assertIsNone(config)

    def test_system_default_returns_no_config(self):
        _, config = self.gateway.resolve_provider(request_provider=None, user_settings=None)
        self.assertIsNone(config)


class ListProvidersTests(unittest.TestCase):
    """The catalogue consumed by the settings UI."""

    def setUp(self):
        self.gateway = _gateway()
        self.providers = self.gateway.list_providers()
        self.by_id = {p["id"]: p for p in self.providers}

    def test_every_provider_is_listed_exactly_once(self):
        self.assertEqual(len(self.providers), len(LLMProviderType))
        self.assertEqual(sorted(self.by_id), sorted(p.value for p in LLMProviderType))

    def test_providers_are_listed_in_declaration_order(self):
        self.assertEqual([p["id"] for p in self.providers], [p.value for p in LLMProviderType])

    def test_ids_are_the_enum_values_and_names_are_title_cased(self):
        github = self.by_id["github_models"]
        self.assertEqual(github["id"], "github_models")
        self.assertEqual(github["name"], "Github Models")

    def test_backends_are_reported_as_wire_strings(self):
        self.assertEqual(self.by_id["github_models"]["backend"], "cli_agent")
        self.assertEqual(self.by_id["ollama"]["backend"], "llm_chain")

    def test_model_suggestions_are_published_for_every_provider(self):
        for provider in LLMProviderType:
            self.assertEqual(self.by_id[provider.value]["models"], MODEL_SUGGESTIONS[provider])
            self.assertTrue(self.by_id[provider.value]["models"])

    def test_ollama_is_advertised_as_requiring_no_api_key(self):
        ollama = self.by_id["ollama"]
        self.assertEqual(ollama["description"], "Local Ollama models (no API key required)")
        self.assertIs(ollama["requires_api_key"], False)

    def test_github_models_mentions_the_token(self):
        github = self.by_id["github_models"]
        self.assertEqual(github["description"], "GitHub Models API (requires GITHUB_TOKEN)")
        self.assertIs(github["requires_api_key"], True)

    def test_hosted_providers_require_a_key_and_use_a_generic_description(self):
        for provider_id in ("openai", "anthropic", "google", "groq"):
            entry = self.by_id[provider_id]
            self.assertEqual(entry["description"], f"{entry['name']} API")
            self.assertIs(entry["requires_api_key"], True)


class GetDefaultModelTests(unittest.TestCase):
    """Default-model lookup."""

    def setUp(self):
        self.gateway = _gateway()

    def test_default_model_is_the_first_suggestion(self):
        for provider in LLMProviderType:
            self.assertEqual(
                self.gateway.get_default_model(provider), MODEL_SUGGESTIONS[provider][0]
            )

    def test_known_default(self):
        self.assertEqual(self.gateway.get_default_model(LLMProviderType.OPENAI), "gpt-4.1")

    def test_unmapped_provider_has_no_default(self):
        self.assertIsNone(self.gateway.get_default_model("mystery-llm"))


class GetLLMGatewaySingletonTests(unittest.TestCase):
    """Process-wide gateway instance."""

    def tearDown(self):
        llm_gateway._gateway_instance = None

    def test_first_call_constructs_and_second_call_reuses(self):
        llm_gateway._gateway_instance = None
        fake_class = MagicMock(name="LLMGateway")
        with patch.object(llm_gateway, "LLMGateway", fake_class):
            first = get_llm_gateway()
            second = get_llm_gateway()

        self.assertIs(first, second)
        self.assertIs(first, fake_class.return_value)
        fake_class.assert_called_once_with()

    def test_existing_instance_is_returned_without_rebuilding_settings(self):
        sentinel = object()
        llm_gateway._gateway_instance = sentinel
        with patch.object(
            llm_gateway, "LLMGateway", side_effect=AssertionError("must not rebuild")
        ):
            self.assertIs(get_llm_gateway(), sentinel)


class GatewaySettingsCaptureTests(unittest.TestCase):
    """The gateway snapshots ``get_settings()`` once, at construction."""

    def test_settings_are_read_from_the_module_level_factory(self):
        settings = SimpleNamespace(default_ai_backend=AIBackend.LLM_CHAIN)
        with patch.object(llm_gateway, "get_settings", return_value=settings) as factory:
            gateway = LLMGateway()
        factory.assert_called_once_with()
        self.assertIs(gateway.settings, settings)
        self.assertIs(gateway.settings.default_ai_backend, AIBackend.LLM_CHAIN)


if __name__ == "__main__":
    unittest.main()
