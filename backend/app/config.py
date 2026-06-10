"""Who sits on the AI Council — via OpenRouter.

One OPENROUTER_API_KEY gives access to every model on the menu. The panel
is just a comma-separated list of OpenRouter model slugs in ROUNDTABLE_PANEL;
display names and provider chips are derived from the slug, or override
them inline with "slug|Display Name".

Examples:
  ROUNDTABLE_PANEL=anthropic/claude-sonnet-4.6,openai/gpt-5.2,google/gemini-3.1-pro,z-ai/glm-5.1,moonshotai/kimi-k2.5
  ROUNDTABLE_PANEL=z-ai/glm-5.1|GLM-5.1,deepseek/deepseek-v3.2|DeepSeek V3.2

Direct provider keys are no longer needed; anything OpenRouter serves is a
one-line panel change.
"""

import os
from dataclasses import dataclass

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

DEFAULT_PANEL = ",".join([
    "anthropic/claude-sonnet-4.6",
    "openai/gpt-5.2",
    "google/gemini-3.1-pro",
    "z-ai/glm-5.1",
    "moonshotai/kimi-k2.5",
])


@dataclass(frozen=True)
class Panelist:
    name: str        # display name on the chip, e.g. "Claude Sonnet 4.6"
    provider: str    # short tag from the slug, e.g. "anthropic"
    model: object    # pydantic-ai model instance


def _openrouter_model(slug: str) -> OpenAIChatModel:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set.")
    return OpenAIChatModel(
        slug,
        provider=OpenAIProvider(base_url=OPENROUTER_BASE_URL, api_key=key),
    )


def _display_name(slug: str) -> str:
    # "moonshotai/kimi-k2.5" -> "Kimi K2.5"
    tail = slug.split("/", 1)[-1]
    words = tail.replace("-", " ").split()
    return " ".join(w.upper() if w.replace(".", "").isdigit() or len(w) <= 3
                    else w.capitalize() for w in words)


def build_panel() -> list[Panelist]:
    raw = os.getenv("ROUNDTABLE_PANEL", DEFAULT_PANEL)
    panel: list[Panelist] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        slug, _, name = entry.partition("|")
        slug = slug.strip()
        panel.append(Panelist(
            name=name.strip() or _display_name(slug),
            provider=slug.split("/", 1)[0],
            model=_openrouter_model(slug),
        ))
    if not panel:
        raise RuntimeError("ROUNDTABLE_PANEL is empty.")
    return panel


def framer_model(panel: list[Panelist]):
    """Framer/summarizer: cheap and fast. Defaults to Haiku via OpenRouter,
    override with FRAMER_MODEL (any OpenRouter slug)."""
    slug = os.getenv("FRAMER_MODEL", "anthropic/claude-haiku-4.5")
    return _openrouter_model(slug)
