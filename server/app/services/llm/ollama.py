from __future__ import annotations

import json

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.llm import tools as T

MAX_ROUNDS = 4
# Local models on CPU are slow; a 7B answering a tool round can take a while.
TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)


class OllamaError(RuntimeError):
    pass


def base_url() -> str:
    return settings.ollama_base_url.rstrip("/")


async def list_models() -> tuple[bool, list[str]]:
    """-> (reachable, model_names).

    These are two different failures and must not be conflated: a reachable
    Ollama with nothing pulled needs `ollama pull`, while an unreachable one
    needs the service started. Returning a bare empty list made both look the
    same and sent you chasing the wrong problem.
    """
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{base_url()}/api/tags")
            resp.raise_for_status()
            return True, [m["name"] for m in resp.json().get("models", [])]
    except Exception:  # noqa: BLE001 - "not reachable" is a normal state here
        return False, []


async def chat(
    session: AsyncSession, system_prompt: str, history: list[dict], message: str
) -> dict:
    messages = [{"role": "system", "content": system_prompt}]
    messages += [
        {"role": m["role"], "content": m["content"]} for m in history if m.get("content")
    ]
    messages.append({"role": "user", "content": message})

    used: list[str] = []

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for _ in range(MAX_ROUNDS):
            try:
                resp = await client.post(
                    f"{base_url()}/api/chat",
                    json={
                        "model": settings.ollama_model,
                        "messages": messages,
                        "tools": T.to_openai_schema(),
                        "stream": False,
                        "options": {
                            "temperature": 0.2,
                            # Ollama defaults to a 4096-token window. The tool
                            # schemas, the system prompt, a full insights
                            # payload and a few turns of history overrun that,
                            # and an overrun is silently truncated from the
                            # front - dropping the system prompt or the very
                            # numbers being asked about, with no error. The
                            # model would then answer confidently from
                            # nothing. Cost is roughly half a gigabyte.
                            "num_ctx": 8192,
                        },
                    },
                )
            except httpx.ConnectError as exc:
                raise OllamaError(
                    f"Cannot reach Ollama at {base_url()}. Is it running? "
                    f"({exc})"
                ) from exc
            except httpx.ReadTimeout as exc:
                raise OllamaError(
                    "Ollama timed out. A 7B model on CPU can take several minutes "
                    "for the first response while the model loads."
                ) from exc

            if resp.status_code == 404:
                raise OllamaError(
                    f"Model '{settings.ollama_model}' is not pulled. "
                    f"Run: ollama pull {settings.ollama_model}"
                )
            if resp.status_code >= 400:
                raise OllamaError(f"Ollama returned {resp.status_code}: {resp.text[:400]}")

            msg = resp.json().get("message") or {}
            calls = msg.get("tool_calls") or []

            if not calls:
                text = (msg.get("content") or "").strip()
                return {"reply": text or "(empty response)", "tools_used": used}

            messages.append(msg)
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                # Some models hand back the arguments as a JSON string.
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                used.append(name)
                result = await T.execute(session, name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

    raise OllamaError("The model kept calling tools without answering; giving up.")


async def chat_plain(prompt: str, timeout_s: float = 600.0) -> str:
    """One-shot completion with no tools, for the title classifier."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=timeout_s, write=30.0, pool=5.0)
    ) as client:
        try:
            resp = await client.post(
                f"{base_url()}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.0, "num_ctx": 8192},
                },
            )
        except httpx.ConnectError as exc:
            raise OllamaError(f"Cannot reach Ollama at {base_url()}.") from exc
        except httpx.ReadTimeout as exc:
            raise OllamaError("Ollama timed out while classifying.") from exc

    if resp.status_code >= 400:
        raise OllamaError(f"Ollama returned {resp.status_code}: {resp.text[:300]}")
    return (resp.json().get("message") or {}).get("content", "")
