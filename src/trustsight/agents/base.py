"""Agent runtime: Bedrock-backed, schema-constrained.

Every agent that emits structured data validates against a pydantic model
before the result is allowed into the pipeline. Free-text parsing of model
output is not acceptable in a system whose selling point is reproducibility.

Model, prompt and playbook versions are recorded on every call so a run can
be replayed (spec s7.1).
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = os.getenv("TRUSTSIGHT_MODEL", "anthropic.claude-sonnet-4-5-20250929-v1:0")
DEFAULT_REGION = os.getenv("AWS_REGION", "eu-west-2")


@dataclass
class AgentCall:
    """One model invocation, recorded for the evidence chain."""

    agent: str
    model_id: str
    prompt_version: str
    playbook_version: str | None
    input_hash: str
    attempts: int
    ok: bool
    error: str | None = None
    raw: str | None = None


@dataclass
class LLMClient:
    """Thin Bedrock wrapper. Swappable for tests and local runs."""

    model_id: str = DEFAULT_MODEL
    region: str = DEFAULT_REGION
    max_tokens: int = 4096
    _client: Any = field(default=None, repr=False)

    def _bedrock(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def invoke(self, system: str, user: str, *, images: list[bytes] | None = None) -> str:
        content: list[dict[str, Any]] = []
        for img in images or []:
            import base64

            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(img).decode(),
                    },
                }
            )
        content.append({"type": "text", "text": user})

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        resp = self._bedrock().invoke_model(
            modelId=self.model_id, body=json.dumps(body)
        )
        payload = json.loads(resp["body"].read())
        return "".join(
            block.get("text", "") for block in payload.get("content", [])
        )


class StructuredAgent(ABC, Generic[T]):
    """Base class for any agent that must emit a validated structure."""

    name: str = "agent"
    prompt_version: str = "v1"
    output_model: type[T]
    max_repair_attempts: int = 2

    def __init__(self, llm: LLMClient | None = None, playbook_version: str | None = None):
        self.llm = llm or LLMClient()
        self.playbook_version = playbook_version

    @abstractmethod
    def system_prompt(self) -> str: ...

    @abstractmethod
    def user_prompt(self, payload: dict[str, Any]) -> str: ...

    def run(self, payload: dict[str, Any], images: list[bytes] | None = None) -> tuple[T | None, AgentCall]:
        import hashlib

        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

        schema = json.dumps(self.output_model.model_json_schema(), indent=2)
        system = (
            f"{self.system_prompt()}\n\n"
            "Reply with a single JSON object and nothing else. No prose, no "
            "markdown fences. It must validate against this JSON Schema:\n"
            f"{schema}\n\n"
            "Any fact the source does not state must be null. Never infer, "
            "never substitute a typical value, never round a number that was "
            "not written down."
        )
        user = self.user_prompt(payload)
        last_error: str | None = None
        raw = None

        for attempt in range(1, self.max_repair_attempts + 2):
            try:
                raw = self.llm.invoke(system, user, images=images)
                text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
                obj = self.output_model.model_validate_json(text)
                return obj, AgentCall(
                    agent=self.name,
                    model_id=self.llm.model_id,
                    prompt_version=self.prompt_version,
                    playbook_version=self.playbook_version,
                    input_hash=digest,
                    attempts=attempt,
                    ok=True,
                    raw=raw,
                )
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:800]
                user = (
                    f"{self.user_prompt(payload)}\n\n"
                    f"Your previous reply was rejected: {last_error}\n"
                    "Return only the corrected JSON object."
                )

        return None, AgentCall(
            agent=self.name,
            model_id=self.llm.model_id,
            prompt_version=self.prompt_version,
            playbook_version=self.playbook_version,
            input_hash=digest,
            attempts=self.max_repair_attempts + 1,
            ok=False,
            error=last_error,
            raw=raw,
        )
