from __future__ import annotations

import json

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.llm import tools as T

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
MAX_ROUNDS = 4


class GeminiError(RuntimeError):
    pass


def available() -> bool:
    return bool(settings.gemini_api_key)


def _explain(status: int, raw: str) -> str:
    """Turn a Gemini HTTP error into something worth reading.

    The raw payloads are verbose and bury the one sentence that matters, and
    the fix is usually account-side rather than anything in this app.
    """
    try:
        err = json.loads(raw).get("error", {})
        message = err.get("message", "").strip()
    except (json.JSONDecodeError, AttributeError):
        message = raw[:300]

    hints = {
        400: "The request was rejected. If this started after a model change, the new model may not accept the same options.",
        403: (
            "Google is refusing this project access to text generation. The API key itself is "
            "valid (listing models works), so this is an account matter, not a bug here: "
            "enable billing on the project, check that the Generative Language API is turned on, "
            "and note the free tier is not offered in every country. "
            "Switch the assistant to the local Ollama provider to keep working meanwhile."
        ),
        404: (
            f"The model '{settings.gemini_model}' does not exist or was retired. "
            "Set GEMINI_MODEL in .env to a current one - 'gemini-flash-latest' tracks the "
            "newest flash model automatically."
        ),
        429: "Rate limit or quota exhausted. Wait, or use the local Ollama provider.",
        500: "Google-side error. Retry in a moment.",
        503: "Gemini is overloaded. Retry in a moment.",
    }
    hint = hints.get(status, "")
    return f"Gemini returned {status}. {message}" + (f"\n\n{hint}" if hint else "")


async def chat(
    session: AsyncSession, system_prompt: str, history: list[dict], message: str
) -> dict:
    if not available():
        raise GeminiError("GEMINI_API_KEY is not set. Add it to .env and restart the stack.")

    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in history
        if m.get("content")
    ]
    contents.append({"role": "user", "parts": [{"text": message}]})

    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "tools": T.to_gemini_schema(),
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1400},
    }

    url = f"{API_ROOT}/models/{settings.gemini_model}:generateContent"
    used: list[str] = []

    async with httpx.AsyncClient(timeout=90) as client:
        for _ in range(MAX_ROUNDS):
            resp = await client.post(
                url,
                json=body,
                headers={"x-goog-api-key": settings.gemini_api_key},
            )
            if resp.status_code >= 400:
                raise GeminiError(_explain(resp.status_code, resp.text))

            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise GeminiError("Gemini returned no candidates (possibly a safety block).")

            parts = candidates[0].get("content", {}).get("parts") or []
            calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not calls:
                # Thinking models return their reasoning as parts flagged
                # `thought`. Those are not the answer and must not be shown.
                text = "".join(
                    p.get("text", "") for p in parts if not p.get("thought")
                ).strip()
                if not text:
                    reason = candidates[0].get("finishReason", "")
                    if reason and reason != "STOP":
                        raise GeminiError(f"Gemini stopped early (finishReason={reason}).")
                return {"reply": text or "(empty response)", "tools_used": used}

            # Echo the model turn back, then answer every call it made.
            body["contents"].append({"role": "model", "parts": parts})
            responses = []
            for call in calls:
                name = call.get("name", "")
                args = call.get("args") or {}
                used.append(name)
                result = await T.execute(session, name, args)
                responses.append(
                    {"functionResponse": {"name": name, "response": {"result": result}}}
                )
            body["contents"].append({"role": "user", "parts": responses})

    raise GeminiError("Gemini kept calling tools without answering; giving up.")


async def chat_plain(prompt: str) -> str:
    """One-shot completion with no tools, for the title classifier."""
    if not available():
        raise GeminiError("GEMINI_API_KEY is not set.")

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{API_ROOT}/models/{settings.gemini_model}:generateContent",
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 2000},
            },
            headers={"x-goog-api-key": settings.gemini_api_key},
        )

    if resp.status_code >= 400:
        raise GeminiError(_explain(resp.status_code, resp.text))

    candidates = resp.json().get("candidates") or []
    if not candidates:
        raise GeminiError("Gemini returned no candidates.")
    parts = candidates[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))
