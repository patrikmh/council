"""Who sits on the Rabble — via OpenRouter.

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
    slug: str        # full OpenRouter slug, e.g. "anthropic/claude-sonnet-4.6"
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


def _parse_panel(raw: str) -> list[Panelist]:
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
            slug=slug,
            model=_openrouter_model(slug),
        ))
    return panel


def build_panel() -> list[Panelist]:
    panel = _parse_panel(os.getenv("ROUNDTABLE_PANEL", DEFAULT_PANEL))
    if not panel:
        raise RuntimeError("ROUNDTABLE_PANEL is empty.")
    return panel


# The news council is its own, smaller table: four voices picked for
# viewpoint diversity — different labs, different countries — separate
# from the poll/debate panel. The coordinator (desk editor + judge)
# deliberately does NOT sit on the panel, so the published ruling stays
# brand-neutral.
DEFAULT_NEWS_PANEL = ",".join([
    "openai/gpt-5.5",
    "x-ai/grok-4.3",
    "z-ai/glm-5.2",
    "mistralai/mistral-medium-3-5",
])


def build_news_panel() -> list[Panelist]:
    panel = _parse_panel(os.getenv("NEWS_PANEL", DEFAULT_NEWS_PANEL))
    if not panel:
        raise RuntimeError("NEWS_PANEL is empty.")
    return panel


def news_coordinator_model():
    """Desk editor + judge for news editions. Sonnet 5 by default,
    override with NEWS_COORDINATOR_MODEL (any OpenRouter slug)."""
    return _openrouter_model(
        os.getenv("NEWS_COORDINATOR_MODEL", "anthropic/claude-sonnet-5"))


def framer_model(panel: list[Panelist]):
    """Framer/summarizer: defaults to Sonnet 5 via OpenRouter,
    override with FRAMER_MODEL (any OpenRouter slug)."""
    slug = os.getenv("FRAMER_MODEL", "anthropic/claude-sonnet-5")
    return _openrouter_model(slug)


def judge_model(panel: list[Panelist]):
    """Debate judge: weighs argument quality after the final round.
    Defaults to the framer's model, override with JUDGE_MODEL."""
    slug = os.getenv("JUDGE_MODEL") or os.getenv(
        "FRAMER_MODEL", "anthropic/claude-haiku-4.5")
    return _openrouter_model(slug)
