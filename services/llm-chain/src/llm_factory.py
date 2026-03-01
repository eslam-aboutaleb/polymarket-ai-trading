"""
LLM Factory - Provider-agnostic LLM initialization.
Supports OpenAI, Anthropic, Google, Groq, and Ollama.
"""
from typing import Optional, List, Any
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel

from src.config import get_settings, LLMProvider


def create_llm(
    streaming: bool = False,
    callbacks: Optional[List[BaseCallbackHandler]] = None
) -> BaseChatModel:
    """
    Factory function to create an LLM based on configuration.
    
    Args:
        streaming: Enable streaming responses
        callbacks: Optional callback handlers
    
    Returns:
        Configured LLM instance
    
    Raises:
        ValueError: If provider is not supported or API key is missing
    """
    settings = get_settings()
    provider = settings.llm_provider
    
    common_kwargs = {
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "streaming": streaming,
    }
    
    if callbacks:
        common_kwargs["callbacks"] = callbacks
    
    if provider == LLMProvider.OPENAI:
        return _create_openai_llm(settings, common_kwargs)
    elif provider == LLMProvider.ANTHROPIC:
        return _create_anthropic_llm(settings, common_kwargs)
    elif provider == LLMProvider.GOOGLE:
        return _create_google_llm(settings, common_kwargs)
    elif provider == LLMProvider.GROQ:
        return _create_groq_llm(settings, common_kwargs)
    elif provider == LLMProvider.OLLAMA:
        return _create_ollama_llm(settings, common_kwargs)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


def _create_openai_llm(settings, kwargs) -> BaseChatModel:
    """Create OpenAI ChatGPT instance."""
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for OpenAI provider")
    
    from langchain_openai import ChatOpenAI
    
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        **kwargs
    )


def _create_anthropic_llm(settings, kwargs) -> BaseChatModel:
    """Create Anthropic Claude instance."""
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is required for Anthropic provider")
    
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        raise ImportError(
            "langchain-anthropic not installed. "
            "Install with: pip install langchain-anthropic"
        )
    
    return ChatAnthropic(
        model=settings.llm_model,
        api_key=settings.anthropic_api_key,
        **kwargs
    )


def _create_google_llm(settings, kwargs) -> BaseChatModel:
    """Create Google Gemini instance."""
    if not settings.google_api_key:
        raise ValueError("GOOGLE_API_KEY is required for Google provider")
    
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError:
        raise ImportError(
            "langchain-google-genai not installed. "
            "Install with: pip install langchain-google-genai"
        )
    
    return ChatGoogleGenerativeAI(
        model=settings.llm_model,
        google_api_key=settings.google_api_key,
        **kwargs
    )


def _create_groq_llm(settings, kwargs) -> BaseChatModel:
    """Create Groq instance (fast inference)."""
    if not settings.groq_api_key:
        raise ValueError("GROQ_API_KEY is required for Groq provider")
    
    try:
        from langchain_groq import ChatGroq
    except ImportError:
        raise ImportError(
            "langchain-groq not installed. "
            "Install with: pip install langchain-groq"
        )
    
    return ChatGroq(
        model=settings.llm_model,
        api_key=settings.groq_api_key,
        **kwargs
    )


def _create_ollama_llm(settings, kwargs) -> BaseChatModel:
    """Create Ollama instance (local models)."""
    try:
        from langchain_community.chat_models import ChatOllama
    except ImportError:
        raise ImportError(
            "langchain-community not installed. "
            "Install with: pip install langchain-community"
        )
    
    # Ollama doesn't use max_tokens the same way
    kwargs.pop("max_tokens", None)
    
    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.ollama_base_url,
        **kwargs
    )


def get_provider_info() -> dict:
    """Get information about the configured LLM provider."""
    settings = get_settings()
    return {
        "provider": settings.llm_provider.value,
        "model": settings.llm_model,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }


# Model suggestions per provider
MODEL_SUGGESTIONS = {
    LLMProvider.OPENAI: [
        "gpt-4-turbo-preview",
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-3.5-turbo",
    ],
    LLMProvider.ANTHROPIC: [
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
    ],
    LLMProvider.GOOGLE: [
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gemini-pro",
    ],
    LLMProvider.GROQ: [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
    ],
    LLMProvider.OLLAMA: [
        "llama3.2",
        "llama3.1",
        "mistral",
        "codellama",
        "phi3",
    ],
}
