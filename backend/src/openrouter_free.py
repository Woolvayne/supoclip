"""
Smart router and JSON schema sanitizer for OpenRouter ``:free`` models.

OpenRouter's ``:free`` endpoints differ from paid endpoints in three ways:
1. They require ``provider.data_collection = "allow"`` (``"deny"`` returns HTTP 404).
2. Several ``:free`` providers reject ``tool_choice = "required"`` or strict
   JSON-schema mode when combined with reasoning tokens, returning JSON inside
   assistant text or using ``tool_choice = "auto"``.
3. Individual ``:free`` upstreams frequently hit transient 429/503 capacity
   limits, so rotating through ``NVIDIA -> Qwen -> Mistral -> other :free``
   keeps self-hosted clip generation reliable at zero API cost.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence

import httpx
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

logger = logging.getLogger(__name__)

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
CATALOG_CACHE_TTL_SECONDS = 900.0

DEFAULT_FREE_MODEL_CHAIN: tuple[str, ...] = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "qwen/qwen3-coder:free",
    "qwen/qwen3-235b-a22b:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "openrouter/free",
)

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

_catalog_cache: tuple[float, List[str]] = (0.0, [])


def is_free_openrouter_model(provider_model_name: Optional[str]) -> bool:
    """True when the OpenRouter model ID targets free-tier endpoints."""
    if not provider_model_name:
        return False
    normalized = provider_model_name.strip().lower()
    return normalized == "openrouter/free" or normalized.endswith(":free")


def _vendor_priority(model_id: str, context_length: int) -> tuple[int, int]:
    lower = model_id.lower()
    if lower.startswith("nvidia/"):
        tier = 0
    elif lower.startswith("qwen/"):
        tier = 1
    elif lower.startswith("mistralai/"):
        tier = 2
    elif lower.startswith(("google/", "meta-llama/", "deepseek/")):
        tier = 3
    else:
        tier = 4
    return (tier, -int(context_length or 0))


async def discover_free_models(
    primary_model: str,
    fallback_model: Optional[str] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> List[str]:
    """Build an ordered ``NVIDIA -> Qwen -> Mistral -> other :free`` candidate list."""
    global _catalog_cache
    now = time.monotonic()
    cached_at, cached_models = _catalog_cache
    discovered: List[str] = []

    if cached_models and (now - cached_at) < CATALOG_CACHE_TTL_SECONDS:
        discovered = list(cached_models)
    else:
        owns_client = client is None
        http = client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await http.get(f"{OPENROUTER_API_BASE}/models")
            if resp.status_code == 200:
                payload = resp.json()
                candidates: List[tuple[tuple[int, int], str]] = []
                for item in payload.get("data") or []:
                    if not isinstance(item, dict):
                        continue
                    model_id = str(item.get("id") or "").strip()
                    if not model_id.endswith(":free"):
                        continue
                    ctx = int(item.get("context_length") or 0)
                    if ctx < 32000:
                        continue
                    arch = item.get("architecture") or {}
                    out_mods = arch.get("output_modalities") or ["text"]
                    if "text" not in out_mods:
                        continue
                    candidates.append((_vendor_priority(model_id, ctx), model_id))
                candidates.sort(key=lambda pair: pair[0])
                discovered = [model_id for _, model_id in candidates[:12]]
                if discovered:
                    _catalog_cache = (now, discovered)
        except Exception as exc:  # noqa: BLE001 - best-effort discovery
            logger.debug("OpenRouter free catalog lookup skipped (%s)", type(exc).__name__)
        finally:
            if owns_client:
                await http.aclose()

    ordered: List[str] = []

    def _add(name: Optional[str]) -> None:
        if not name:
            return
        cleaned = name.strip()
        if cleaned and cleaned != "openrouter/free" and cleaned not in ordered:
            ordered.append(cleaned)

    if primary_model and primary_model.strip().lower() != "openrouter/free":
        _add(primary_model)
    if fallback_model and is_free_openrouter_model(fallback_model):
        _add(fallback_model)
    for model_id in discovered:
        _add(model_id)
    for model_id in DEFAULT_FREE_MODEL_CHAIN:
        _add(model_id)
    if "openrouter/free" not in ordered:
        ordered.append("openrouter/free")
    return ordered


def extract_json_object(text: str) -> Any:
    """Extract a JSON object or array from model text (stripping reasoning/fences)."""
    if not text:
        return None
    cleaned = _THINK_TAG_RE.sub("", text).strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except ValueError:
        pass

    for match in _FENCE_RE.finditer(cleaned):
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except ValueError:
            continue

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(cleaned[idx:])
            return obj
        except ValueError:
            continue
    return None


def _resolve_schema(
    schema: Dict[str, Any], root_schema: Dict[str, Any]
) -> Dict[str, Any]:
    if "$ref" in schema:
        ref = str(schema["$ref"])
        if ref.startswith("#/$defs/"):
            target = (root_schema.get("$defs") or {}).get(ref.split("/")[-1])
            if isinstance(target, dict):
                return _resolve_schema(target, root_schema)
    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        non_null = [
            branch
            for branch in schema["anyOf"]
            if isinstance(branch, dict) and branch.get("type") != "null"
        ]
        if non_null:
            return _resolve_schema(non_null[0], root_schema)
    return schema


def sanitize_for_schema(
    data: Any,
    schema: Dict[str, Any],
    root_schema: Optional[Dict[str, Any]] = None,
) -> Any:
    """Normalize common free-LLM quirks (float integers, out-of-range scores, aliases)."""
    if not isinstance(schema, dict):
        return data
    root = root_schema or schema
    resolved = _resolve_schema(schema, root)
    schema_type = resolved.get("type")

    if data is None:
        if "default" in resolved:
            return resolved["default"]
        return None

    if "enum" in resolved and isinstance(resolved["enum"], list):
        allowed = resolved["enum"]
        if data in allowed:
            return data
        if isinstance(data, str):
            lowered = data.strip().lower()
            for item in allowed:
                if isinstance(item, str) and item.lower() == lowered:
                    return item
        return resolved.get("default", allowed[0] if allowed else data)

    if schema_type == "integer":
        try:
            val = int(round(float(data)))
        except (TypeError, ValueError):
            val = int(resolved.get("default", 0))
        if "minimum" in resolved:
            val = max(int(resolved["minimum"]), val)
        if "maximum" in resolved:
            val = min(int(resolved["maximum"]), val)
        return val

    if schema_type == "number":
        try:
            val_f = float(data)
        except (TypeError, ValueError):
            val_f = float(resolved.get("default", 0.0))
        if (
            resolved.get("maximum") == 1.0
            and resolved.get("minimum") == 0.0
            and 1.0 < val_f <= 100.0
        ):
            val_f = val_f / 100.0
        if "minimum" in resolved:
            val_f = max(float(resolved["minimum"]), val_f)
        if "maximum" in resolved:
            val_f = min(float(resolved["maximum"]), val_f)
        return val_f

    if schema_type == "boolean":
        if isinstance(data, bool):
            return data
        if isinstance(data, str):
            return data.strip().lower() in {"true", "1", "yes"}
        return bool(data)

    if schema_type == "string":
        return data if isinstance(data, str) else str(data)

    if schema_type == "array":
        if not isinstance(data, list):
            return []
        item_schema = resolved.get("items") or {}
        return [sanitize_for_schema(item, item_schema, root) for item in data]

    if schema_type == "object" or "properties" in resolved:
        if not isinstance(data, dict):
            return data
        props = resolved.get("properties") or {}
        required = set(resolved.get("required") or [])
        out = dict(data)
        # Alias recovery common in TranscriptSegment / TranscriptAnalysis
        if "text" in props and "text" not in out and "segment" in out:
            out["text"] = out.pop("segment")
        if "hook_title" in props and "hook_title" not in out:
            for alias in ("title", "headline"):
                if alias in out:
                    out["hook_title"] = out.get(alias)
                    break
        for key, prop_schema in props.items():
            if key in out:
                out[key] = sanitize_for_schema(out[key], prop_schema, root)
            elif key in required:
                prop_resolved = _resolve_schema(prop_schema, root)
                if "default" in prop_resolved:
                    out[key] = prop_resolved["default"]
                elif prop_resolved.get("type") == "array":
                    out[key] = []
                elif prop_resolved.get("type") == "string":
                    out[key] = ""
                elif prop_resolved.get("type") == "object":
                    out[key] = sanitize_for_schema({}, prop_resolved, root)
        return out

    return data


def _convert_messages(
    messages: Sequence[ModelMessage],
    model_request_parameters: ModelRequestParameters,
) -> List[Dict[str, Any]]:
    chat_messages: List[Dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, SystemPromptPart):
                    if part.content:
                        chat_messages.append({"role": "system", "content": part.content})
                elif isinstance(part, UserPromptPart):
                    content = (
                        part.content
                        if isinstance(part.content, str)
                        else json.dumps(part.content, ensure_ascii=False, default=str)
                    )
                    chat_messages.append({"role": "user", "content": content})
                elif isinstance(part, ToolReturnPart):
                    chat_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": part.tool_call_id,
                            "content": part.model_response_str(),
                        }
                    )
                elif isinstance(part, RetryPromptPart):
                    if part.tool_name:
                        chat_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": part.tool_call_id,
                                "content": part.model_response(),
                            }
                        )
                    else:
                        chat_messages.append(
                            {"role": "user", "content": part.model_response()}
                        )
        elif isinstance(msg, ModelResponse):
            text_chunks: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content:
                    text_chunks.append(part.content)
                elif isinstance(part, ToolCallPart):
                    args_str = (
                        part.args
                        if isinstance(part.args, str)
                        else json.dumps(part.args or {}, ensure_ascii=False)
                    )
                    tool_calls.append(
                        {
                            "id": part.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": part.tool_name,
                                "arguments": args_str,
                            },
                        }
                    )
            assistant_entry: Dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(text_chunks) if text_chunks else "",
            }
            if tool_calls:
                assistant_entry["tool_calls"] = tool_calls
            chat_messages.append(assistant_entry)

    # Instruction parts on ModelRequestParameters (pydantic-ai >= 1.x)
    for instr in getattr(model_request_parameters, "instruction_parts", None) or []:
        text = getattr(instr, "content", None)
        if text:
            chat_messages.insert(0, {"role": "system", "content": str(text)})

    return chat_messages


class FreeOpenRouterSmartModel(Model):
    """PydanticAI Model with automatic OpenRouter ``:free`` routing and JSON repair."""

    def __init__(
        self,
        primary_model: str,
        *,
        api_key: str,
        fallback_model: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        super().__init__()
        self._primary_model = primary_model.strip()
        self._fallback_model = fallback_model
        self._api_key = api_key
        self._http_client = http_client

    @property
    def model_name(self) -> str:
        return self._primary_model

    @property
    def system(self) -> str:
        return "openrouter"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        candidates = await discover_free_models(
            self._primary_model,
            self._fallback_model,
            client=self._http_client,
        )
        chat_messages = _convert_messages(messages, model_request_parameters)

        all_tools = list(model_request_parameters.function_tools) + list(
            model_request_parameters.output_tools
        )
        tool_schemas: Dict[str, Dict[str, Any]] = {
            t.name: (t.parameters_json_schema or {}) for t in all_tools
        }
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.parameters_json_schema or {"type": "object", "properties": {}},
                },
            }
            for t in all_tools
        ]

        output_tool = (
            model_request_parameters.output_tools[0]
            if model_request_parameters.output_tools
            else None
        )
        output_obj = model_request_parameters.output_object

        effective_messages = list(chat_messages)
        if output_tool is not None:
            schema_hint = json.dumps(output_tool.parameters_json_schema, ensure_ascii=False)
            effective_messages = [
                *effective_messages,
                {
                    "role": "system",
                    "content": (
                        f"Call the `{output_tool.name}` tool or output ONLY a valid JSON "
                        f"object matching this JSON schema: {schema_hint}"
                    ),
                },
            ]
        elif output_obj is not None and output_obj.json_schema:
            schema_hint = json.dumps(output_obj.json_schema, ensure_ascii=False)
            effective_messages = [
                *effective_messages,
                {
                    "role": "system",
                    "content": f"Output ONLY a valid JSON object matching this schema: {schema_hint}",
                },
            ]

        max_tokens = int((model_settings or {}).get("max_tokens") or 8192)
        timeout_s = float((model_settings or {}).get("timeout") or 180.0)

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=timeout_s)
        last_status = 503
        last_error_body = "All OpenRouter free models failed"

        try:
            for candidate in candidates[:8]:
                payload: Dict[str, Any] = {
                    "model": candidate,
                    "messages": effective_messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "provider": {
                        "allow_fallbacks": True,
                        "data_collection": "allow",
                    },
                }
                if openai_tools:
                    payload["tools"] = openai_tools
                    payload["tool_choice"] = "auto"

                try:
                    response = await client.post(
                        f"{OPENROUTER_API_BASE}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://supoclip.com",
                            "X-Title": "SupoClip",
                        },
                        json=payload,
                    )
                except httpx.HTTPError as exc:
                    last_error_body = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "OpenRouter free candidate %s network error (%s), trying next",
                        candidate,
                        type(exc).__name__,
                    )
                    continue

                if response.status_code != 200:
                    last_status = response.status_code
                    last_error_body = response.text[:500]
                    # 401 means the API key itself is invalid; fail immediately.
                    if response.status_code == 401:
                        raise ModelHTTPError(
                            status_code=401,
                            model_name=candidate,
                            body=last_error_body,
                        )
                    # If a specific model doesn't support tool declarations (400/404),
                    # retry once as pure JSON generation when only an output_tool is needed.
                    if (
                        response.status_code in {400, 404}
                        and openai_tools
                        and output_tool is not None
                        and not model_request_parameters.function_tools
                    ):
                        fallback_payload = {
                            k: v
                            for k, v in payload.items()
                            if k not in {"tools", "tool_choice"}
                        }
                        retry_resp = await client.post(
                            f"{OPENROUTER_API_BASE}/chat/completions",
                            headers={
                                "Authorization": f"Bearer {self._api_key}",
                                "Content-Type": "application/json",
                                "HTTP-Referer": "https://supoclip.com",
                                "X-Title": "SupoClip",
                            },
                            json=fallback_payload,
                        )
                        if retry_resp.status_code == 200:
                            response = retry_resp
                        else:
                            logger.info(
                                "OpenRouter free candidate %s returned HTTP %s, trying next",
                                candidate,
                                response.status_code,
                            )
                            continue
                    else:
                        logger.info(
                            "OpenRouter free candidate %s returned HTTP %s, trying next",
                            candidate,
                            response.status_code,
                        )
                        continue

                try:
                    body = response.json()
                except ValueError:
                    continue
                choices = body.get("choices") or []
                if not choices:
                    continue
                message = choices[0].get("message") or {}
                raw_tool_calls = message.get("tool_calls") or []
                content_text = str(message.get("content") or "").strip()

                parts: List[ModelResponsePart] = []
                for idx, tc in enumerate(raw_tool_calls):
                    fn = tc.get("function") or {}
                    t_name = str(fn.get("name") or (output_tool.name if output_tool else ""))
                    if not t_name:
                        continue
                    raw_args = fn.get("arguments")
                    parsed_args: Any = raw_args
                    if isinstance(raw_args, str):
                        parsed_args = extract_json_object(raw_args)
                        if parsed_args is None:
                            parsed_args = raw_args
                    if isinstance(parsed_args, dict) and t_name in tool_schemas:
                        parsed_args = sanitize_for_schema(parsed_args, tool_schemas[t_name])
                    parts.append(
                        ToolCallPart(
                            tool_name=t_name,
                            args=parsed_args,
                            tool_call_id=str(tc.get("id") or f"call_{idx}"),
                        )
                    )

                if not parts and output_tool is not None and content_text:
                    extracted = extract_json_object(content_text)
                    if isinstance(extracted, dict):
                        sanitized = sanitize_for_schema(
                            extracted, output_tool.parameters_json_schema or {}
                        )
                        parts.append(
                            ToolCallPart(
                                tool_name=output_tool.name,
                                args=sanitized,
                                tool_call_id="call_json_fallback",
                            )
                        )

                if not parts and content_text:
                    cleaned_text = _THINK_TAG_RE.sub("", content_text).strip()
                    if output_obj is not None and output_obj.json_schema:
                        extracted = extract_json_object(cleaned_text)
                        if isinstance(extracted, dict):
                            cleaned_text = json.dumps(
                                sanitize_for_schema(extracted, output_obj.json_schema),
                                ensure_ascii=False,
                            )
                    if cleaned_text:
                        parts.append(TextPart(content=cleaned_text))

                if not parts:
                    logger.info(
                        "OpenRouter free candidate %s returned empty output, trying next",
                        candidate,
                    )
                    continue

                usage_raw = body.get("usage") or {}
                usage = RequestUsage(
                    input_tokens=int(usage_raw.get("prompt_tokens") or 0),
                    output_tokens=int(usage_raw.get("completion_tokens") or 0),
                )
                resolved_model = str(body.get("model") or candidate)
                logger.info("OpenRouter free smart-router succeeded via %s", resolved_model)
                return ModelResponse(
                    parts=parts,
                    usage=usage,
                    model_name=resolved_model,
                    provider_name="openrouter",
                )
        finally:
            if owns_client:
                await client.aclose()

        raise ModelHTTPError(
            status_code=last_status,
            model_name=self._primary_model,
            body=last_error_body,
        )
