from ..config import Settings
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .stub import StubProvider


def build_llm(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case "stub":
            return StubProvider(
                lag_range=(settings.stub_lag_min_s, settings.stub_lag_max_s),
                tokens_per_s=(settings.stub_tokens_per_s_min, settings.stub_tokens_per_s_max),
                stall_probability=settings.stub_stall_probability,
            )
        case "anthropic":
            return AnthropicProvider.anthropic(model=settings.llm_model, max_tokens=settings.llm_max_tokens)
        case "azure":
            if not settings.azure_resource:
                raise RuntimeError("APP_AZURE_RESOURCE is required for the azure provider")
            return AnthropicProvider.azure(
                resource=settings.azure_resource,
                api_key=settings.azure_api_key,
                model=settings.llm_model,
                max_tokens=settings.llm_max_tokens,
            )
    raise ValueError(f"unknown llm provider {settings.llm_provider!r}")
