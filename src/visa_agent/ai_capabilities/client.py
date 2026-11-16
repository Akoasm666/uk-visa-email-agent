"""SiliconFlow OpenAI-compatible chat client (injectable for tests)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from visa_agent.config import Settings
from visa_agent.domain.enums import ErrorCode
from visa_agent.domain.errors import DomainError


class ChatClient(Protocol):
    def complete(
        self, *, messages: list[dict[str, str]], model: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SiliconFlowClient:
    api_key: str
    base_url: str
    timeout_s: float = 60.0
    max_tokens: int = 1200

    @classmethod
    def from_settings(
        cls, settings: Settings, *, timeout_s: float = 60.0, max_tokens: int = 1200,
    ) -> SiliconFlowClient:
        if not settings.model_configured:
            raise DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message="model API is not configured",
            )
        return cls(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            timeout_s=timeout_s,
            max_tokens=max_tokens,
        )

    def complete(
        self, *, messages: list[dict[str, str]], model: str
    ) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0.2,
                        "max_tokens": self.max_tokens,
                        "stream": False,
                    },
                )
        except httpx.HTTPError as exc:
            raise DomainError(
                code=ErrorCode.SYSTEM_BLOCKED,
                message="model provider request failed",
                details=(str(exc),),
            ) from exc
        if resp.status_code >= 400:
            raise DomainError(
                code=ErrorCode.SYSTEM_BLOCKED,
                message="model provider returned an error",
                details=(str(resp.status_code), resp.text[:500]),
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise DomainError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="model provider returned non-JSON body",
            ) from exc


def complete_json_locally(
    messages: list[dict[str, str]], *, max_tokens: int = 1200, schema: dict | None = None,
) -> dict[str, Any]:
    """Use the installed local model for structured tasks when remote chat fails."""
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                "http://127.0.0.1:11434/api/chat",
                timeout=httpx.Timeout(45, connect=2),
                json={
                    "model": "qwen3-vl:2b-instruct", "stream": False, "format": schema or "json",
                    "options": {"temperature": 0, "num_predict": max_tokens}, "messages": messages,
                },
            )
            response.raise_for_status()
            message = response.json()["message"]
            return {"choices": [{"message": message}]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise DomainError(ErrorCode.SYSTEM_BLOCKED, "local model request failed") from exc
