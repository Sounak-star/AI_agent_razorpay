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

# ── Failover ──────────────────────────────────────────────────────────────────
#
# Each provider key carries its own tokens-per-minute quota. A second key is
# therefore additional capacity, not a second attempt at the same exhausted
# budget — which is the whole reason moving to one is legitimate where a plain
# retry is not.
#
# Failover is deliberately narrow. It happens when a key is rate limited or
# rejected, because another key can answer both. It does NOT happen on a
# timeout or a connection failure: those are properties of the endpoint or the
# network, identical for every key, and cycling through keys would just
# multiply the wait before reporting the same problem.

# Exceptions where a different key is worth trying, by class name — matched by
# name so this module does not depend on the SDK's exception hierarchy.
FAILOVER_ON = frozenset({
    "RateLimitError",          # this key is out of quota; another has its own
    "AuthenticationError",     # this key is bad or revoked
    "PermissionDeniedError",   # this key lacks access to the model
})


def resolve_all() -> list[LLMConfig]:
    """
    Every usable provider config, primary first.

    Only Groq supports extra keys today; the list is still the return type so
    callers do not have to care how many there are.
    """
    primary = resolve()
    configs = [primary]

    if primary.provider == "groq":
        extra = [
            key.strip()
            for key in (settings.GROQ_API_KEYS_FALLBACK or "").split(",")
            if key.strip()
        ]
        seen = {primary.api_key}
        for key in extra:
            if key in seen:
                continue          # a duplicate is not a second quota
            seen.add(key)
            configs.append(
                LLMConfig(
                    provider=primary.provider,
                    model=primary.model,
                    base_url=primary.base_url,
                    api_key=key,
                )
            )
    return configs


def _client_for(cfg: LLMConfig):
    return openai.OpenAI(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=0,          # a retry would multiply the deadline
    )


def key_fingerprint(api_key: str) -> str:
    """
    A stable, non-reversible label for a key, safe to write to the ledger.

    The ledger is append-only and gets read by people, so the key itself must
    never reach it; the last four characters plus a length are enough to tell
    two keys apart when reading a failover entry.
    """
    import hashlib

    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]
    return f"key_{digest}"


def complete_with_failover(
    *,
    messages: list[dict],
    max_tokens: int,
    on_failover=None,
):
    """
    Run a completion, moving to the next key when this one cannot answer.

    Returns (response, config, attempt_index). Raises the last error when every
    key has been tried — the caller still sees a real failure, so a exhausted
    pool is never mistaken for a working one.

    `on_failover(from_cfg, error, next_index)` is called before each move, so
    the caller can record it against a session. Nothing here writes to the
    ledger: this module has no session to write against.
    """
    configs = resolve_all()
    last_error: Exception | None = None

    for index, cfg in enumerate(configs):
        client = _client_for(cfg)
        try:
            response = client.chat.completions.create(
                model=cfg.model,
                max_tokens=max_tokens,
                messages=messages,
            )
            return response, cfg, index
        except Exception as exc:                                  # noqa: BLE001
            last_error = exc
            if type(exc).__name__ not in FAILOVER_ON:
                raise                       # not something another key can fix
            if index + 1 >= len(configs):
                raise                       # no key left; the caller reports it
            if on_failover is not None:
                on_failover(cfg, exc, index + 1)

    raise last_error if last_error else LLMNotConfigured("no provider configured")
