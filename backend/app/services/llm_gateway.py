"""
LLM Gateway - Factory pattern for LLM provider selection.

This module implements the Factory design pattern to enable runtime selection
of LLM providers. It acts as a gateway between the backend API and the various
LLM services (LLM-chain for OpenAI/Anthropic/Google/Groq/Ollama, CLI-agent for
GitHub Models).

The gateway supports:
- Per-request provider selection (override user defaults)
- User preference-based provider selection
- System default fallback
- Provider availability checking
"""
from enum import Enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from app.config import get_settings, AIBackend


class LLMProviderType(str, Enum):
    """
    Available LLM providers across all services.
    
    These map to:
    - LLM-chain service (port 50051): OPENAI, ANTHROPIC, GOOGLE, GROQ, OLLAMA
    - CLI-agent service (port 50052): GITHUB_MODELS
    """
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    GROQ = "groq"
    OLLAMA = "ollama"
    GITHUB_MODELS = "github_models"


# Map provider types to proto enum values (must match analysis.proto LLMProvider enum)
PROVIDER_TO_PROTO = {
    LLMProviderType.OPENAI: 1,       # LLM_PROVIDER_OPENAI
    LLMProviderType.ANTHROPIC: 2,    # LLM_PROVIDER_ANTHROPIC
    LLMProviderType.GOOGLE: 3,       # LLM_PROVIDER_GOOGLE
    LLMProviderType.GROQ: 4,         # LLM_PROVIDER_GROQ
    LLMProviderType.OLLAMA: 5,       # LLM_PROVIDER_OLLAMA
    LLMProviderType.GITHUB_MODELS: 6, # LLM_PROVIDER_GITHUB
}

# Map provider types to the backend service that handles them
PROVIDER_TO_BACKEND = {
    LLMProviderType.OPENAI: AIBackend.LLM_CHAIN,
    LLMProviderType.ANTHROPIC: AIBackend.LLM_CHAIN,
    LLMProviderType.GOOGLE: AIBackend.LLM_CHAIN,
    LLMProviderType.GROQ: AIBackend.LLM_CHAIN,
    LLMProviderType.OLLAMA: AIBackend.LLM_CHAIN,
    LLMProviderType.GITHUB_MODELS: AIBackend.CLI_AGENT,
}


# Model suggestions per provider (for UI display)
MODEL_SUGGESTIONS = {
    LLMProviderType.OPENAI: [
        "gpt-4-turbo-preview",
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-3.5-turbo",
    ],
    LLMProviderType.ANTHROPIC: [
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
    ],
    LLMProviderType.GOOGLE: [
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gemini-pro",
    ],
    LLMProviderType.GROQ: [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
    ],
    LLMProviderType.OLLAMA: [
        "llama3.2",
        "llama3.1",
        "mistral",
        "codellama",
        "phi3",
    ],
    LLMProviderType.GITHUB_MODELS: [
        "gpt-4o-mini",
        "gpt-4o",
    ],
}


@dataclass
class LLMConfig:
    """
    Configuration for LLM request.
    
    This is passed to the AnalysisClient to configure the LLM for a specific request.
    """
    provider: Optional[LLMProviderType] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    
    def to_proto_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for gRPC request."""
        result = {}
        if self.provider:
            result["provider"] = PROVIDER_TO_PROTO.get(self.provider, 0)
        if self.model:
            result["model"] = self.model
        if self.temperature is not None and self.temperature > 0:
            result["temperature"] = self.temperature
        if self.max_tokens is not None and self.max_tokens > 0:
            result["max_tokens"] = self.max_tokens
        return result


class LLMGateway:
    """
    Factory/Gateway for LLM provider selection.
    
    Implements the Factory pattern to:
    1. Determine which backend service to use based on provider
    2. Create appropriate LLM configuration for requests
    3. Provide provider availability information
    
    Usage:
        gateway = LLMGateway()
        
        # Get backend and config for a provider
        backend, config = gateway.get_backend_for_provider(LLMProviderType.OLLAMA)
        client = AnalysisClient(backend=backend)
        result = await client.analyze_market(..., llm_config=config)
        
        # Or use convenience method with user settings
        backend, config = gateway.resolve_provider(
            request_provider="ollama",
            user_settings=user_settings_record
        )
    """
    
    def __init__(self):
        self.settings = get_settings()
    
    def get_backend_for_provider(
        self,
        provider: LLMProviderType,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None
    ) -> tuple[AIBackend, LLMConfig]:
        """
        Get the appropriate backend and config for a provider.
        
        Args:
            provider: The LLM provider to use
            model: Optional model override
            temperature: Optional temperature override
            max_tokens: Optional max_tokens override
        
        Returns:
            Tuple of (AIBackend, LLMConfig)
        """
        backend = PROVIDER_TO_BACKEND.get(provider, AIBackend.LLM_CHAIN)
        config = LLMConfig(
            provider=provider,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens
        )
        return backend, config
    
    def resolve_provider(
        self,
        request_provider: Optional[str] = None,
        request_model: Optional[str] = None,
        user_settings = None,
        default_backend: Optional[AIBackend] = None
    ) -> tuple[AIBackend, Optional[LLMConfig]]:
        """
        Resolve which provider to use based on priority:
        1. Request-level override (if specified)
        2. User settings preference (if available)
        3. System default
        
        Args:
            request_provider: Provider specified in the request (highest priority)
            request_model: Model specified in the request
            user_settings: User's settings record (for preferred_llm_provider)
            default_backend: Fallback backend if nothing else specified
        
        Returns:
            Tuple of (AIBackend, Optional[LLMConfig])
        """
        # Priority 1: Request-level override
        if request_provider:
            try:
                provider = LLMProviderType(request_provider.lower())
                return self.get_backend_for_provider(provider, model=request_model)
            except ValueError:
                pass  # Invalid provider, fall through to next priority
        
        # Priority 2: User settings preference
        if user_settings:
            # Check for new preferred_llm_provider field
            preferred_provider = getattr(user_settings, 'preferred_llm_provider', None)
            if preferred_provider:
                try:
                    provider = LLMProviderType(preferred_provider.lower())
                    preferred_model = getattr(user_settings, 'preferred_llm_model', None)
                    return self.get_backend_for_provider(provider, model=preferred_model)
                except ValueError:
                    pass
            
            # Fall back to legacy ai_backend field
            ai_backend = getattr(user_settings, 'ai_backend', None)
            if ai_backend:
                from app.models.user_settings import AIBackendType
                if ai_backend == AIBackendType.CLI_AGENT.value:
                    return AIBackend.CLI_AGENT, None
                elif ai_backend == AIBackendType.LLM_CHAIN.value:
                    return AIBackend.LLM_CHAIN, None
        
        # Priority 3: System default
        backend = default_backend or self.settings.default_ai_backend
        return backend, None
    
    def list_providers(self) -> List[Dict[str, Any]]:
        """
        List all available LLM providers with their configuration status.
        
        Returns:
            List of provider info dictionaries
        """
        providers = []
        
        for provider in LLMProviderType:
            provider_info = {
                "id": provider.value,
                "name": provider.value.replace("_", " ").title(),
                "backend": PROVIDER_TO_BACKEND.get(provider, AIBackend.LLM_CHAIN).value,
                "models": MODEL_SUGGESTIONS.get(provider, []),
            }
            
            # Add provider-specific info
            if provider == LLMProviderType.OLLAMA:
                provider_info["description"] = "Local Ollama models (no API key required)"
                provider_info["requires_api_key"] = False
            elif provider == LLMProviderType.GITHUB_MODELS:
                provider_info["description"] = "GitHub Models API (requires GITHUB_TOKEN)"
                provider_info["requires_api_key"] = True
            else:
                provider_info["description"] = f"{provider_info['name']} API"
                provider_info["requires_api_key"] = True
            
            providers.append(provider_info)
        
        return providers
    
    def get_default_model(self, provider: LLMProviderType) -> Optional[str]:
        """Get the default model for a provider."""
        models = MODEL_SUGGESTIONS.get(provider, [])
        return models[0] if models else None


# Singleton instance
_gateway_instance: Optional[LLMGateway] = None


def get_llm_gateway() -> LLMGateway:
    """Get or create the LLM gateway singleton."""
    global _gateway_instance
    if _gateway_instance is None:
        _gateway_instance = LLMGateway()
    return _gateway_instance
