"""Small LLM abstraction: Gemini, OpenAI-compatible HTTP, or offline mode."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests


class LLMError(Exception):
    pass


class OfflineError(LLMError):
    pass


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise LLMError(f"Could not parse JSON from LLM response: {text[:300]}")


def _quota_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(
        token in s
        for token in (
            "429",
            "quota",
            "rate limit",
            "resource_exhausted",
            "too many requests",
        )
    )


class LLM:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        lc = cfg.get("llm", {})
        self.requested_provider = str(lc.get("provider", "auto")).lower()
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.base_url = str(lc.get("openai_base_url") or "").rstrip("/")
        self.temperature = float(lc.get("temperature", 0.3))
        self.min_interval = float(lc.get("min_seconds_between_calls", 6))
        self.max_retries = int(lc.get("max_retries", 3))
        self._last_call = 0.0
        self._client = None
        self.provider = self._resolve_provider()
        self.model = (
            lc.get("gemini_model")
            if self.provider == "gemini"
            else lc.get("openai_model")
        )

    def _resolve_provider(self) -> str:
        p = self.requested_provider
        if p == "auto":
            if self.gemini_key:
                return "gemini"
            if self.openai_key:
                return "openai"
            return "offline"
        if p == "gemini":
            return "gemini" if self.gemini_key else "offline"
        if p in {"openai", "openai-compatible"}:
            return "openai" if self.openai_key else "offline"
        return "offline"

    @property
    def available(self) -> bool:
        return self.provider in {"gemini", "openai"}

    def describe(self) -> str:
        if self.provider == "offline":
            return "offline heuristics (no API key configured)"
        return f"{self.provider}:{self.model}"

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.time()

    def _gemini_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.gemini_key)
        return self._client

    def _call_gemini(self, prompt: str, json_mode: bool) -> str:
        client = self._gemini_client()
        config: dict[str, Any] = {"temperature": self.temperature}
        if json_mode:
            config["response_mime_type"] = "application/json"
        resp = client.models.generate_content(
            model=self.model, contents=prompt, config=config
        )
        text = getattr(resp, "text", None)
        if not text:
            raise LLMError("Gemini returned an empty response")
        return text

    def _call_openai(self, prompt: str, json_mode: bool) -> str:
        if not self.base_url:
            raise LLMError("llm.openai_base_url is not configured")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.openai_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )
        if resp.status_code == 400 and json_mode:
            # Some compatible providers do not support response_format.
            payload.pop("response_format", None)
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            )
        if resp.status_code >= 400:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response: {str(data)[:300]}") from exc

    def generate(self, prompt: str, json_mode: bool = False) -> str:
        if not self.available:
            raise OfflineError("No LLM provider configured")
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                if self.provider == "gemini":
                    return self._call_gemini(prompt, json_mode)
                return self._call_openai(prompt, json_mode)
            except Exception as exc:  # noqa: BLE001 - provider errors are broad
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                delay = 20 * attempt if _quota_error(exc) else 3 * attempt
                time.sleep(delay)
        raise LLMError(f"LLM call failed after {self.max_retries} attempts: {last_exc}")

    def generate_json(self, prompt: str) -> dict:
        text = self.generate(prompt, json_mode=True)
        return _extract_json(text)


def llm_status(cfg: dict) -> str:
    llm = LLM(cfg)
    if llm.available:
        return f"LLM ready: {llm.describe()}"
    return (
        "LLM offline: set GEMINI_API_KEY (aistudio.google.com/apikey) "
        "or OPENAI_API_KEY in .env for tailored writing and smarter scoring."
    )
