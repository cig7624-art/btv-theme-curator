"""Response counters for the current browser session, never a billing ledger.

Only returned SDK responses can be counted here. A failed request, SDK retry or
parsing exception may still incur costs that must be checked in OpenAI billing.
No prompts, credentials, response text, identifiers or user data are retained.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping


SESSION_KEY = "api_response_usage"
FEATURES = frozenset({"director", "weekly_themes", "content_candidates", "theme_search"})


def _counter(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _session_state():
    # CLI collectors and unit tests must not create Streamlit's mock global
    # session state, which would mix counters between unrelated requests.
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx(suppress_warning=True) is None:
            return None
        import streamlit as st
        return st.session_state
    except Exception:
        return None


def read_session_usage(state: MutableMapping | None = None) -> dict:
    """Return an independent numeric summary; reading never increments usage."""
    target = state if state is not None else _session_state()
    raw = target.get(SESSION_KEY, {}) if target is not None else {}
    if not isinstance(raw, Mapping):
        raw = {}
    features = raw.get("features", {})
    if not isinstance(features, Mapping):
        features = {}
    inputs = _counter(raw.get("input_tokens"))
    outputs = _counter(raw.get("output_tokens"))
    return {
        "response_count": _counter(raw.get("response_count")),
        "input_tokens": inputs,
        "output_tokens": outputs,
        "cached_input_tokens": min(inputs, _counter(raw.get("cached_input_tokens"))),
        "total_tokens": inputs + outputs,
        "unmetered_response_count": _counter(raw.get("unmetered_response_count")),
        "features": {key: _counter(features.get(key)) for key in sorted(FEATURES)},
    }


def record_llm_response(response, *, feature: str, model: str, state: MutableMapping | None = None):
    """Record a returned response without allowing telemetry to break generation.

    ``state`` can be injected for isolated tests. Production uses only the current
    Streamlit session. Unknown models have no invented price attached to them.
    """
    del model  # Model identifiers are unnecessary for numeric session counters.
    if response is None or feature not in FEATURES:
        return
    try:
        target = state if state is not None else _session_state()
        if target is None:
            return
        current = read_session_usage(target)
        usage = _field(response, "usage")
        raw_inputs = _field(usage, "input_tokens")
        raw_outputs = _field(usage, "output_tokens")
        inputs = _counter(raw_inputs)
        outputs = _counter(raw_outputs)
        details = _field(usage, "input_tokens_details")
        cached = min(inputs, _counter(_field(details, "cached_tokens")))
        metered = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                      for value in (raw_inputs, raw_outputs))
        current["response_count"] += 1
        current["features"][feature] += 1
        current["input_tokens"] += inputs
        current["output_tokens"] += outputs
        current["cached_input_tokens"] += cached
        current["total_tokens"] = current["input_tokens"] + current["output_tokens"]
        if not metered:
            current["unmetered_response_count"] += 1
        target[SESSION_KEY] = current
    except Exception:
        # Reporting must never suppress an otherwise valid creative result.
        return
