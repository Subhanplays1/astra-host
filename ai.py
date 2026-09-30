"""Gemini AI client — stdlib only (urllib), no extra requirements."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request

from config import GEMINI_API_KEY, GEMINI_MODEL

log = logging.getLogger("vexdeploy.ai")

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_TIMEOUT = 25
# Transient Gemini statuses worth retrying: rate limit / overloaded / server error
RETRY_STATUSES = {429, 500, 502, 503}
RETRY_DELAYS = (1.0, 2.5)  # backoff seconds before 2nd/3rd attempt


class AIError(RuntimeError):
    """Gemini call failed (network, quota, safety block, bad key)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def is_configured() -> bool:
    return bool(GEMINI_API_KEY)


def _generate(
    model: str, prompt: str, system: str | None, timeout: int
) -> str:
    """Single blocking generateContent call. Raises AIError on any failure."""
    if not GEMINI_API_KEY:
        raise AIError("GEMINI_API_KEY is not set")
    url = f"{API_BASE}/{model}:generateContent?key={GEMINI_API_KEY}"
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 1500,
            "topP": 0.95,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise AIError(f"Gemini HTTP {exc.code}: {detail}", status=exc.code) from exc
    except AIError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface any transport error to caller
        raise AIError(f"Gemini request failed: {exc}") from exc

    try:
        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
    except (IndexError, AttributeError, TypeError):
        text = ""
    if not text:
        raise AIError(f"empty Gemini response: {json.dumps(data)[:200]}")
    return text


def _call(
    prompt: str,
    system: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Call Gemini with retired-model switching + backoff for transient errors.

    Total wall-clock budget is `timeout` seconds (including sleeps), so async
    wait_for() wrappers with a slightly larger timeout always win.
    """
    if not GEMINI_API_KEY:
        raise AIError("GEMINI_API_KEY is not set")
    model = GEMINI_MODEL
    start = time.monotonic()
    delay_idx = 0
    switched = False
    last_exc: AIError | None = None
    while True:
        remaining = timeout - (time.monotonic() - start)
        if remaining < 3:
            break
        try:
            return _generate(model, prompt, system, int(max(3, remaining)))
        except AIError as exc:
            last_exc = exc
            # 404 retired model — follow Google's suggestion once.
            if exc.status == 404 and not switched:
                m = re.search(r"use models/([A-Za-z0-9._-]+)", str(exc))
                if m and m.group(1) != model:
                    log.warning(
                        "model %s unavailable — retrying with %s", model, m.group(1)
                    )
                    model = m.group(1)
                    switched = True
                    continue
            # 429/5xx overload — short backoff, then retry within budget.
            if exc.status in RETRY_STATUSES and delay_idx < len(RETRY_DELAYS):
                nap = RETRY_DELAYS[delay_idx]
                delay_idx += 1
                if time.monotonic() - start + nap > timeout:
                    break
                log.warning(
                    "Gemini HTTP %s — retrying in %.1fs (%s)",
                    exc.status,
                    nap,
                    model,
                )
                time.sleep(nap)
                continue
            raise
    assert last_exc is not None
    raise last_exc


async def chat(prompt: str, system: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Async wrapper around the blocking Gemini call."""
    return await asyncio.to_thread(_call, prompt, system, timeout)


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (tolerates fences/prose)."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE)
    m = re.search(r"[\[{]", cleaned)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(cleaned[m.start() :])
        return obj
    except ValueError:
        return None
