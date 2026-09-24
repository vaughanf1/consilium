"""Which models exist, who serves them, and what key each provider needs.

Kept deliberately small and editable. Any model id not listed here still works:
pass `--model provider:model-id` (e.g. `openai:my-fine-tune`, `ollama:llama3.1`).
"""

from __future__ import annotations

# (display name, model id, provider)
MODELS: list[tuple[str, str, str]] = [
    ("Claude Opus 5", "claude-opus-5", "anthropic"),
    ("Claude Sonnet 5", "claude-sonnet-5", "anthropic"),
    ("Claude Haiku 4.5", "claude-haiku-4-5-20251001", "anthropic"),
    ("GPT-5.6", "gpt-5.6", "openai"),
    ("DeepSeek V4 Pro", "deepseek-v4-pro", "deepseek"),
    ("Grok 4.3", "grok-4.3", "xai"),
    ("Llama 3.1 (local, free)", "llama3.1", "ollama"),
    ("Qwen 3 (local, free)", "qwen3", "ollama"),
]

# When OpenRouter is the ONLY configured provider, this is what the analysts
# reason with unless CONSILIUM_MODEL says otherwise. Chosen for cost: a long
# backtest makes thousands of analyst calls, and a flagship model there is the
# difference between cents and tens of dollars. Override for quality.
OPENROUTER_DEFAULT = "openai/gpt-6-luna"

# OpenRouter is a gateway: one key, hundreds of models from every vendor. Its
# catalogue moves weekly, so we never hardcode slugs — `openrouter_models()`
# asks the live endpoint. These are only the fallback if the fetch fails.
OPENROUTER_FALLBACK = [
    ("anthropic/claude-sonnet-4.5", "Claude Sonnet 4.5"),
    ("openai/gpt-4o", "GPT-4o"),
    ("google/gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("meta-llama/llama-3.3-70b-instruct", "Llama 3.3 70B"),
    ("deepseek/deepseek-chat", "DeepSeek Chat"),
]

PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama": None,  # local, keyless
}

OPENAI_COMPATIBLE_BASE = {
    "openai": None,
    "deepseek": "https://api.deepseek.com",
    "xai": "https://api.x.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
}


def provider_for(model_id: str) -> str:
    if ":" in model_id:
        return model_id.split(":", 1)[0]
    for _, mid, prov in MODELS:
        if mid == model_id:
            return prov
    if model_id.startswith("claude"):
        return "anthropic"
    if model_id.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    return "openai"


def env_var_for(provider: str) -> str | None:
    return PROVIDER_ENV.get(provider)


_openrouter_cache: tuple[float, list[tuple[str, str]]] | None = None


def openrouter_models(limit: int = 60, ttl: float = 900.0) -> list[tuple[str, str]]:
    """(model id, display name) from OpenRouter's public catalogue, newest first.

    The endpoint needs no auth and the list changes constantly, so this is
    fetched and cached rather than pinned in code. Any failure falls back to a
    small static list — a slow gateway should cost you the picker, not the app.
    """
    global _openrouter_cache
    import time
    if _openrouter_cache and time.time() - _openrouter_cache[0] < ttl:
        return _openrouter_cache[1]
    models = OPENROUTER_FALLBACK
    try:
        import httpx
        r = httpx.get("https://openrouter.ai/api/v1/models", timeout=8.0)
        r.raise_for_status()
        rows = []
        for m in r.json().get("data", []):
            mid, name = m.get("id"), m.get("name") or m.get("id")
            if not mid or ":" in mid:   # skip :free / :batch / :thinking variants
                continue
            rows.append((mid, name, m.get("created") or 0))
        if rows:
            rows.sort(key=lambda x: -x[2])
            models = [(mid, name) for mid, name, _ in rows[:limit]]
    except Exception:  # offline, rate limited, schema drift — all non-fatal
        pass
    _openrouter_cache = (time.time(), models)
    return models


def list_models() -> list[dict]:
    import os
    out = []
    for display, mid, prov in MODELS:
        env = PROVIDER_ENV.get(prov)
        out.append({"display": display, "id": mid, "provider": prov,
                    "env": env, "configured": (env is None) or bool(os.environ.get(env))})
    if os.environ.get("OPENROUTER_API_KEY"):
        for mid, name in openrouter_models():
            out.append({"display": f"{name} (via OpenRouter)", "id": f"openrouter:{mid}",
                        "provider": "openrouter", "env": "OPENROUTER_API_KEY", "configured": True})
    return out
