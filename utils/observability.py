"""Measured provider-call helpers; usage is recorded only when providers return."""

import time
import random
from typing import Any, Callable

import config
from persistence import record_provider_usage, ensure_external_call_allowed


def invoke_with_usage(
    invoke: Callable[[Any], Any],
    payload: Any,
    *,
    provider: str,
    operation: str,
    run_id: str,
    model: str | None = None,
) -> Any:
    ensure_external_call_allowed(run_id)
    started = time.monotonic()
    attempts = max(0, min(config.MAX_RETRIES, 2))
    retry_count = 0
    while True:
        try:
            result = invoke(payload)
            break
        except Exception as error:
            status = getattr(error, "status_code", None) or getattr(error, "code", None)
            retryable_class = any(
                marker in type(error).__name__.casefold()
                for marker in ("timeout", "serviceunavailable", "resourceexhausted", "internalserver")
            )
            retryable_status = status in {429, 500, 502, 503, 504}
            if retry_count >= attempts or not (retryable_class or retryable_status):
                record_provider_usage(
                    run_id=run_id,
                    provider=provider,
                    operation=operation,
                    model=model,
                    successful=False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    retry_count=retry_count,
                    error=str(error),
                )
                raise
            retry_after = getattr(error, "retry_after", None)
            try:
                delay = min(float(retry_after), 5.0) if retry_after is not None else 0.2 * (2 ** retry_count)
            except (TypeError, ValueError):
                delay = 0.2 * (2 ** retry_count)
            time.sleep(delay + random.uniform(0, 0.15))
            retry_count += 1

    usage = getattr(result, "usage_metadata", None)
    if not isinstance(usage, dict):
        response_metadata = getattr(result, "response_metadata", {}) or {}
        usage = response_metadata.get("token_usage", {})
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
    output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
    if input_tokens is not None:
        input_tokens = int(input_tokens)
    if output_tokens is not None:
        output_tokens = int(output_tokens)
    record_provider_usage(
        run_id=run_id,
        provider=provider,
        operation=operation,
        model=model,
        successful=True,
        duration_ms=int((time.monotonic() - started) * 1000),
        retry_count=retry_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return result
