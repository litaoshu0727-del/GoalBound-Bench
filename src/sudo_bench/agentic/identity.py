"""Canonical, secret-free identities for model-generation requests."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping


def canonical_generation_config(client: Any) -> Dict[str, Any]:
    """Return stable request metadata exposed by a completion client.

    Real API clients expose all output-affecting parameters through
    ``generation_config``. Minimal test/third-party clients fall back to their
    model id, which preserves backwards compatibility without inspecting private
    attributes or ever recording credentials.
    """

    raw = getattr(client, "generation_config", None)
    if callable(raw):
        raw = raw()
    if raw is None:
        raw = {"model": client.model}
    if not isinstance(raw, Mapping):
        raise TypeError("client.generation_config must be a mapping")
    # Round-trip through canonical JSON both validates serialisability and removes
    # custom Mapping implementations whose ordering could be unstable.
    return json.loads(json.dumps(raw, ensure_ascii=False, sort_keys=True))


def canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
