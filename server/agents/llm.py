"""
LLM provider resolution.

Groq and xAI both expose OpenAI-compatible APIs, so the only things that differ
between them are the base URL, the key and the model id. Keeping that in one
place means switching provider is a config change rather than an edit to every
call site — the previous arrangement had the vendor hardcoded in both agents,
which is how one of them ended up pinned to a model that had been retired.

Provider is chosen by LLM_PROVIDER, or inferred from whichever key is set.
Nothing here touches prices, prompts or policy: the agent's output is advisory
in this system regardless of which model produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
import openai

from server.config import settings

# base_url and a sensible default model per provider.
PROVIDERS: dict[str, dict[str, str]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        # Direct instruct model: answers in ~130 output tokens where the
        # reasoning models spend 300-600 getting to the same JSON.
        "default_model": "qwen/qwen3.8-27b",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-3-mini",
    },
}


class LLMNotConfigured(RuntimeError):
    """No usable provider key is set."""


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str


def resolve() -> LLMConfig:
    """
    Work out which provider to call.

    Explicit LLM_PROVIDER wins. Otherwise whichever key is present is used, with
    Groq preferred when both are — it is the one currently funded.
    """
    provider = (settings.LLM_PROVIDER or "").strip().lower()

    if not provider:
        if settings.GROQ_API_KEY:
            provider = "groq"
        elif settings.XAI_API_KEY:
            provider = "xai"
        else:
            raise LLMNotConfigured(
                "No LLM key configured. Set GROQ_API_KEY (or XAI_API_KEY) in .env, "
                "or run with --stub to use recorded fixtures."
            )

    if provider not in PROVIDERS:
        raise LLMNotConfigured(
            f"LLM_PROVIDER={provider!r} is not supported. "
            f"Choose one of: {', '.join(sorted(PROVIDERS))}"
        )

    key = settings.GROQ_API_KEY if provider == "groq" else settings.XAI_API_KEY
    if not key:
        raise LLMNotConfigured(
            f"LLM_PROVIDER={provider} but its API key is empty. "
            f"Set {'GROQ_API_KEY' if provider == 'groq' else 'XAI_API_KEY'} in .env."
        )

    spec = PROVIDERS[provider]
    return LLMConfig(
        provider=provider,
        model=(settings.LLM_MODEL or spec["default_model"]).strip(),
        base_url=spec["base_url"],
        api_key=key,
    )


def get_client_and_model():
    """Return an OpenAI-compatible client pointed at the configured provider."""
    cfg = resolve()
    # The deadline lives on the client so every call site inherits it and none
    # can forget to pass one.
    client = openai.OpenAI(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=0,          # a retry would multiply the deadline
    )
    return client, cfg.model
