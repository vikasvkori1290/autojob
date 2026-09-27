"""Shared LLM client for the job_scraper pipeline.

All pipeline modules import from here. Nothing else should instantiate
an OpenAI client directly.

Configuration is read from environment variables (or a .env file loaded
by the caller):

    NVIDIA_API_KEY  — required; your NVIDIA NIM API key
    NIM_BASE_URL    — optional; defaults to https://integrate.api.nvidia.com/v1
    NIM_MODEL       — optional; defaults to openai/gpt-oss-20b

Usage::

    from job_scraper.lib.llm import chat

    response = chat([{"role": "user", "content": "Hello"}])
    print(response)  # the assistant's reply as a string
"""

import os
from typing import Sequence

try:
    import openai
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "openai package is required. Run: pip install openai>=1.30.0"
    ) from exc

_BASE_URL = "https://integrate.api.nvidia.com/v1"
_DEFAULT_MODEL = "meta/llama-3.2-11b-vision-instruct"

# Module-level singleton; created lazily so tests can patch env vars before first use.
_client: "openai.OpenAI | None" = None


def _get_client() -> "openai.OpenAI":
    """Return the shared OpenAI client, creating it on first call."""
    global _client
    if _client is None:
        api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError(
                "NVIDIA_API_KEY environment variable is not set. "
                "Copy .env.example to .env and fill in your NIM API key."
            )
        base_url = os.environ.get("NIM_BASE_URL", _BASE_URL).rstrip("/")
        _client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=45.0)
    return _client


def reset_client() -> None:
    """Discard the cached client. Used in tests to re-read env vars."""
    global _client
    _client = None


def get_model() -> str:
    """Return the active model name."""
    return os.environ.get("NIM_MODEL", _DEFAULT_MODEL)


def chat(
    messages: Sequence[dict],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> str:
    """Send a chat completion request and return the reply text.

    Args:
        messages:    List of {"role": ..., "content": ...} dicts.
        model:       Override the model for this call only.
                     Defaults to NIM_MODEL env var or openai/gpt-oss-20b.
        temperature: Sampling temperature (default 0.2 for deterministic ranking).
        max_tokens:  Maximum tokens in the reply (default 2048).

    Returns:
        The assistant's reply as a plain string.

    Raises:
        EnvironmentError: If NVIDIA_API_KEY is not set.
        openai.APIError:  On any API-level error (rate limit, auth, etc.).
    """
    client = _get_client()
    response = client.chat.completions.create(
        model=model or get_model(),
        messages=list(messages),
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""
