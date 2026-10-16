"""Intelligence domain: LLM-backed reasoning and forecasting.

Owns the LLM gateway factory, consensus/ensemble scoring, forecast recording
and resolution, model-drift detection, prompt versioning and AI news
generation.

Dependency rule: may import `platform` only. All LLM provider selection goes
through `llm_gateway.LLMGateway`; vendor SDKs must never be imported here.
"""
