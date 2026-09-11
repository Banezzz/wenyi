"""Call LongCat through its OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..transport import Messages, ResolvedModel
from ._openai_compatible import (
    OpenAICompatibleBaseClient,
    base_request_kwargs,
    deep_merge,
)

DEFAULT_BASE_URL = "https://api.longcat.chat/openai/v1"
DEFAULT_API_KEY_ENV = "LONGCAT_API_KEY"
DEFAULT_MODEL = "LongCat-2.0"


class LongCatOptions(BaseModel):
    """LongCat-specific model request options."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    thinking: bool = True
    reasoning_effort: str = "high"
    extra_body: dict[str, Any] = Field(default_factory=dict)


def preset_models() -> dict[str, ResolvedModel[LongCatOptions]]:
    """Return built-in LongCat defaults for strong, cheap and fast tiers."""
    thinking = LongCatOptions()
    mechanical = LongCatOptions(thinking=False)
    return {
        "strong": ResolvedModel(model=DEFAULT_MODEL, options=thinking),
        "cheap": ResolvedModel(model=DEFAULT_MODEL, options=thinking),
        "fast": ResolvedModel(model=DEFAULT_MODEL, options=mechanical),
    }


def build_request_kwargs(
    model_config: ResolvedModel[LongCatOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Convert generic arguments into LongCat thinking-mode request parameters.

    LongCat puts thinking type and effort inside extra_body.thinking, unlike
    DeepSeek's top-level reasoning_effort field.
    """
    kwargs = base_request_kwargs(model_config.model, messages, json_mode=json_mode)
    if model_config.options.thinking:
        extra_body: dict[str, Any] = {
            "thinking": {
                "type": "enabled",
                "effort": model_config.options.reasoning_effort,
            }
        }
    else:
        extra_body = {"thinking": {"type": "disabled"}}
    if model_config.options.extra_body:
        extra_body = deep_merge(extra_body, model_config.options.extra_body)
    kwargs["extra_body"] = extra_body
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return kwargs


class LongCatClient(OpenAICompatibleBaseClient[LongCatOptions]):
    default_base_url = DEFAULT_BASE_URL
    default_api_key_env = DEFAULT_API_KEY_ENV
    requires_api_key = True

    def _build_request_kwargs(
        self,
        model_config: ResolvedModel[LongCatOptions],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Build final request arguments for the selected LongCat tier."""
        return build_request_kwargs(
            model_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
