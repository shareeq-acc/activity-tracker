"""Provider-agnostic entry point for the chat assistant."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import analytics as an
from app.services.llm import gemini, ollama

PROVIDERS = ("gemini", "ollama")


def system_prompt() -> str:
    now = datetime.now(tz=an.local_tz())
    return f"""You are the analyst for a personal computer-activity tracker. \
The user is asking about their own data.

Today is {now.strftime('%A, %d %B %Y')} and the local time is \
{now.strftime('%H:%M')} ({settings.tz}).

WHERE THE DATA COMES FROM
A collector on the user's Windows PC samples the foreground window every few \
seconds and records how long each application and window title held focus. \
Stretches with no keyboard or mouse input are recorded as idle. Rules map each \
window to a category, and categories roll up into buckets:
  - growth      : building, learning
  - distraction : social media, watching, gaming
  - neutral     : communication, browsing, utility
  - idle        : away from the machine

HOW TO ANSWER
- Always call a tool before stating any number. Never estimate, never invent a \
figure, and never reuse a number from earlier in the conversation if the user \
asked about a different period.
- If a tool returns an error or empty data, say so plainly instead of guessing.
- Be direct and concrete. Lead with the answer, then at most a few supporting \
numbers. Short paragraphs or tight bullets, no preamble.
- Time is already formatted like "2h 15m" in tool output. Quote it as given.
- The user wants honest feedback on their habits, so do not flatter. If the \
numbers are bad, say the numbers are bad. If they improved, say so.
- Uncategorized time means rules are missing, not that the time was wasted. \
Say that when it is relevant.
- You have no ability to change settings, edit rules, or start timers. If asked, \
explain where in the UI to do it."""


def provider_status() -> dict:
    return {
        "gemini": {
            "available": gemini.available(),
            "model": settings.gemini_model,
            "detail": "" if gemini.available() else "GEMINI_API_KEY not set",
        },
        "ollama": {
            "available": True,  # reachability is checked lazily, on use
            "model": settings.ollama_model,
            "detail": settings.ollama_base_url,
        },
    }


async def chat(
    session: AsyncSession,
    provider: str,
    history: list[dict],
    message: str,
) -> dict:
    prompt = system_prompt()
    if provider == "gemini":
        result = await gemini.chat(session, prompt, history, message)
    else:
        result = await ollama.chat(session, prompt, history, message)
    result["provider"] = provider
    return result
