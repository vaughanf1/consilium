"""LLM clients — one protocol, two transports (Anthropic; OpenAI-compatible).

Providers raise on transport failure; the committee decides how to degrade
(analysts abstain, the CIO falls back to rules). No provider is ever allowed
to silently return a neutral answer.
"""

from __future__ import annotations

import json
import os
import re
from typing import Protocol, runtime_checkable

from consilium.llm.registry import (
    MODELS, OPENAI_COMPATIBLE_BASE, OPENROUTER_DEFAULT, env_var_for, provider_for,
)

DEFAULT_MODEL = "claude-sonnet-5"


class LLMError(RuntimeError):
    pass


@runtime_checkable
class LLMClient(Protocol):
    model: str

    def complete(self, system: str, user: str) -> str: ...


# Reasoning models spend most of their output budget on a hidden trace before
# writing a word of the answer, so a budget sized for the answer alone returns
# empty content. This is sized for trace + answer.
DEFAULT_MAX_TOKENS = 4000


class AnthropicClient:
    def __init__(self, model: str, api_key: str, max_tokens: int = DEFAULT_MAX_TOKENS, timeout: float = 90.0) -> None:
        import anthropic
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self._max_tokens = max_tokens

    def complete(self, system: str, user: str) -> str:
        try:
            msg = self._client.messages.create(
                model=self.model, max_tokens=self._max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            raise LLMError(f"anthropic/{self.model}: {exc}") from exc
        return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")


class OpenAICompatibleClient:
    def __init__(self, model: str, api_key: str | None, base_url: str | None,
                 max_tokens: int = DEFAULT_MAX_TOKENS, timeout: float = 90.0) -> None:
        import openai
        self.model = model
        self._client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url, timeout=timeout, max_retries=2)
        self._max_tokens = max_tokens

    def complete(self, system: str, user: str) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self.model, max_tokens=self._max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        except Exception as exc:
            raise LLMError(f"{self.model}: {exc}") from exc

        # Gateways can return an in-band error with choices=null instead of a
        # transport failure. Indexing straight in turns that into a cryptic
        # "'NoneType' object is not subscriptable" three frames away.
        choices = getattr(resp, "choices", None)
        if not choices:
            detail = getattr(resp, "error", None) or getattr(resp, "model_extra", {}).get("error") or "no detail given"
            raise LLMError(f"{self.model}: provider returned no choices ({detail})")

        choice = choices[0]
        content = (choice.message.content or "").strip()
        if content:
            return content
        # Empty content from a reasoning model: either it was cut off mid-trace,
        # or the provider put everything in the trace field. Say which — a silent
        # empty string becomes an abstention nobody can explain.
        extra = getattr(choice.message, "model_extra", None) or {}
        trace = (extra.get("reasoning") or "").strip()
        if choice.finish_reason == "length":
            raise LLMError(
                f"{self.model}: hit the {self._max_tokens}-token limit before writing an answer "
                f"({getattr(resp.usage, 'completion_tokens', '?')} tokens, mostly reasoning). "
                "Raise max_tokens or pick a non-reasoning model.")
        if trace:
            return trace          # the answer is often inside the trace; the parser will find it
        raise LLMError(f"{self.model}: empty response (finish_reason={choice.finish_reason})")


def _provider_has_key(provider: str) -> bool:
    """True only for providers that need a key AND have one set. A keyless local
    provider (Ollama) returns False here: it is a deliberate choice, never an
    automatic fallback — auto-selecting it would claim a model is available when
    nothing is listening on localhost."""
    env = env_var_for(provider)
    return bool(env) and bool(os.environ.get(env))


def current_model() -> str:
    """The model the personas reason with.

    CONSILIUM_MODEL always wins — an explicit choice is never second-guessed,
    and a missing key for it should fail loudly rather than silently swap.
    Otherwise: the built-in default if its provider is configured, else the
    first registry model whose provider has a key. So setting any one provider
    key is enough to staff the committee.
    """
    explicit = os.environ.get("CONSILIUM_MODEL")
    if explicit:
        return explicit
    if _provider_has_key(provider_for(DEFAULT_MODEL)):
        return DEFAULT_MODEL
    for _, model_id, provider in MODELS:
        if _provider_has_key(provider):
            return model_id
    if _provider_has_key("openrouter"):
        # One gateway key reaches every vendor, so it is a valid sole provider.
        return f"openrouter:{OPENROUTER_DEFAULT}"
    return DEFAULT_MODEL


def make_llm(model: str | None = None, **kw) -> LLMClient:
    """Build a client for `model` (`provider:id` or a registry id), routed by provider."""
    model = model or current_model()
    provider = provider_for(model)
    model_id = model.split(":", 1)[1] if ":" in model else model
    env = env_var_for(provider)
    key = os.environ.get(env) if env else None
    if env and not key:
        raise LLMError(f"{env} is not set — needed for {provider} model {model_id}")
    if provider == "anthropic":
        return AnthropicClient(model_id, key, **kw)
    base = OPENAI_COMPATIBLE_BASE.get(provider)
    if provider not in OPENAI_COMPATIBLE_BASE:
        base = os.environ.get(f"{provider.upper()}_BASE_URL")
        if not base:
            raise LLMError(f"unknown provider {provider!r}; set {provider.upper()}_BASE_URL for an OpenAI-compatible endpoint")
    return OpenAICompatibleClient(model_id, key, base, **kw)


def llm_available(model: str | None = None) -> bool:
    """Is a key (or a keyless local provider) configured for the chosen model?"""
    model = model or current_model()
    if os.environ.get("CONSILIUM_NO_LLM"):
        return False
    provider = provider_for(model)
    env = env_var_for(provider)
    return env is None or bool(os.environ.get(env))


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a response: fence -> whole -> balanced braces."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"no JSON object in response: {text[:160]!r}")
