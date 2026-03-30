from __future__ import annotations

import importlib
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, cast

REQUEST_TRANSFORM_CATEGORY_INPUT_REDUCTION = "input-reduction"
REQUEST_TRANSFORM_CATEGORY_POLICY = "policy"
REQUEST_TRANSFORM_CATEGORY_ROUTING_HINT = "routing-hint"
REQUEST_TRANSFORM_CATEGORY_OBSERVABILITY = "observability"
REQUEST_TRANSFORM_CATEGORY_RTK = "rtk"
REQUEST_TRANSFORM_CATEGORY_REDACTION = "redaction"
REQUEST_TRANSFORM_CATEGORY_SUMMARIZATION = "summarization"
REQUEST_TRANSFORM_CATEGORY_PROMPT_POLICY = "prompt-policy"
DEFAULT_REQUEST_TRANSFORM_CATEGORY = REQUEST_TRANSFORM_CATEGORY_INPUT_REDUCTION

REQUEST_TRANSFORM_FAIL_OPEN = "fail-open"
REQUEST_TRANSFORM_FAIL_CLOSED = "fail-closed"
DEFAULT_REQUEST_TRANSFORM_FAILURE_POLICY = REQUEST_TRANSFORM_FAIL_OPEN
DEFAULT_REQUEST_TRANSFORM_ORDER = 100


@dataclass(frozen=True)
class RequestTransformContext:
    request_id: str
    local_path: str
    method: str
    provider_id: str
    headers: dict[str, str]


@dataclass(frozen=True)
class RequestTransformMetrics:
    input_chars_before: int | None = None
    input_chars_after: int | None = None
    estimated_tokens_before: int | None = None
    estimated_tokens_after: int | None = None
    saved_tokens_estimate: int | None = None
    input_tokens_saved_estimate: int | None = None
    output_tokens_saved_estimate: int | None = None
    transform_latency_ms: int | None = None


@dataclass(frozen=True)
class RequestTransformResult:
    payload: dict[str, Any]
    applied: bool = False
    annotations: dict[str, Any] = field(default_factory=dict)
    metrics: RequestTransformMetrics | None = None
    affects_routing: bool | None = None
    routing_hints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestTransformTrace:
    plugin_id: str
    category: str
    order: int
    failure_policy: str
    applied: bool
    affects_routing: bool
    annotations: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    routing_hints: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class AppliedRequestTransforms:
    payload: dict[str, Any]
    applied: bool = False
    traces: list[RequestTransformTrace] = field(default_factory=list)
    affects_routing: bool = False
    routing_hints: dict[str, Any] = field(default_factory=dict)

    @property
    def annotations(self) -> dict[str, Any]:
        return {
            "plugins": [trace.plugin_id for trace in self.traces if trace.applied],
            "details": {
                trace.plugin_id: {
                    "category": trace.category,
                    "order": trace.order,
                    "failure_policy": trace.failure_policy,
                    "applied": trace.applied,
                    "affects_routing": trace.affects_routing,
                    "annotations": trace.annotations,
                    "metrics": trace.metrics,
                    "routing_hints": trace.routing_hints,
                    "error": trace.error,
                }
                for trace in self.traces
            },
            "affects_routing": self.affects_routing,
            "routing_hints": self.routing_hints,
        }


class RequestTransformError(RuntimeError):
    def __init__(self, plugin_id: str, message: str) -> None:
        super().__init__(message)
        self.plugin_id = plugin_id


class RequestTransformPlugin(Protocol):
    plugin_id: str

    def applies_to(self, payload: dict[str, Any], context: RequestTransformContext) -> bool: ...

    def transform(self, payload: dict[str, Any], context: RequestTransformContext) -> RequestTransformResult: ...


def list_request_transforms() -> list[RequestTransformPlugin]:
    configured = [item.strip() for item in os.getenv("PUNKRECORDS_REQUEST_TRANSFORM_MODULES", "").split(",") if item.strip()]
    transforms: list[RequestTransformPlugin] = []
    for module_name in configured:
        module = importlib.import_module(module_name)
        single = getattr(module, "REQUEST_TRANSFORM", None)
        many = getattr(module, "REQUEST_TRANSFORMS", None)
        if _is_request_transform(single):
            transforms.append(cast(RequestTransformPlugin, single))
        if isinstance(many, (list, tuple)):
            transforms.extend(cast(RequestTransformPlugin, item) for item in many if _is_request_transform(item))
    return sorted(transforms, key=_transform_sort_key)


def apply_request_transforms(payload: dict[str, Any], context: RequestTransformContext) -> AppliedRequestTransforms:
    current_payload = dict(payload)
    traces: list[RequestTransformTrace] = []
    affects_routing = False
    applied_any = False
    routing_hints: dict[str, Any] = {}

    for plugin in list_request_transforms():
        plugin_id = _plugin_id(plugin)
        category = _plugin_category(plugin)
        order = _plugin_order(plugin)
        failure_policy = _plugin_failure_policy(plugin)
        try:
            if not plugin.applies_to(current_payload, context):
                traces.append(
                    RequestTransformTrace(
                        plugin_id=plugin_id,
                        category=category,
                        order=order,
                        failure_policy=failure_policy,
                        applied=False,
                        affects_routing=False,
                        routing_hints={},
                    )
                )
                continue

            started_at = time.time()
            result = plugin.transform(dict(current_payload), context)
            elapsed_ms = int((time.time() - started_at) * 1000)
            metrics = result.metrics or RequestTransformMetrics(transform_latency_ms=elapsed_ms)
            if metrics.transform_latency_ms is None:
                metrics = RequestTransformMetrics(
                    input_chars_before=metrics.input_chars_before,
                    input_chars_after=metrics.input_chars_after,
                    estimated_tokens_before=metrics.estimated_tokens_before,
                    estimated_tokens_after=metrics.estimated_tokens_after,
                    saved_tokens_estimate=metrics.saved_tokens_estimate,
                    input_tokens_saved_estimate=metrics.input_tokens_saved_estimate,
                    output_tokens_saved_estimate=metrics.output_tokens_saved_estimate,
                    transform_latency_ms=elapsed_ms,
                )

            current_payload = dict(result.payload)
            trace_affects_routing = bool(result.affects_routing if result.affects_routing is not None else _plugin_affects_routing(plugin))
            affects_routing = affects_routing or trace_affects_routing
            applied_any = applied_any or result.applied
            if result.routing_hints:
                routing_hints.update(dict(result.routing_hints))
            traces.append(
                RequestTransformTrace(
                    plugin_id=plugin_id,
                    category=category,
                    order=order,
                    failure_policy=failure_policy,
                    applied=result.applied,
                    affects_routing=trace_affects_routing,
                    annotations=dict(result.annotations),
                    metrics=asdict(metrics),
                    routing_hints=dict(result.routing_hints),
                )
            )
        except Exception as exc:
            trace = RequestTransformTrace(
                plugin_id=plugin_id,
                category=category,
                order=order,
                failure_policy=failure_policy,
                applied=False,
                affects_routing=False,
                routing_hints={},
                error=str(exc),
            )
            traces.append(trace)
            if failure_policy == REQUEST_TRANSFORM_FAIL_CLOSED:
                raise RequestTransformError(plugin_id, str(exc)) from exc

    return AppliedRequestTransforms(payload=current_payload, applied=applied_any, traces=traces, affects_routing=affects_routing, routing_hints=routing_hints)


def _is_request_transform(value: object) -> bool:
    return hasattr(value, "plugin_id") and hasattr(value, "applies_to") and hasattr(value, "transform")


def _transform_sort_key(plugin: RequestTransformPlugin) -> tuple[int, str]:
    return (_plugin_order(plugin), _plugin_id(plugin))


def _plugin_id(plugin: RequestTransformPlugin) -> str:
    value = str(getattr(plugin, "plugin_id", "")).strip()
    return value or plugin.__class__.__name__


def _plugin_category(plugin: RequestTransformPlugin) -> str:
    value = str(getattr(plugin, "category", DEFAULT_REQUEST_TRANSFORM_CATEGORY)).strip()
    return value or DEFAULT_REQUEST_TRANSFORM_CATEGORY


def _plugin_order(plugin: RequestTransformPlugin) -> int:
    value = getattr(plugin, "order", DEFAULT_REQUEST_TRANSFORM_ORDER)
    return value if isinstance(value, int) else DEFAULT_REQUEST_TRANSFORM_ORDER


def _plugin_failure_policy(plugin: RequestTransformPlugin) -> str:
    value = str(getattr(plugin, "failure_policy", DEFAULT_REQUEST_TRANSFORM_FAILURE_POLICY)).strip()
    if value in {REQUEST_TRANSFORM_FAIL_OPEN, REQUEST_TRANSFORM_FAIL_CLOSED}:
        return value
    return DEFAULT_REQUEST_TRANSFORM_FAILURE_POLICY


def _plugin_affects_routing(plugin: RequestTransformPlugin) -> bool:
    return bool(getattr(plugin, "affects_routing", False))
