"""Ceiling wave: from a rested window, fire 30 simultaneous tiny requests per model.

If the per-model per-minute allowance is N, roughly N succeed and the rest come back
429 carrying X-RateLimit-Limit / -Reset, which names the exact limit and window.
Runs the distinct models in parallel (separate buckets) and reports the headers.
"""

import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://openrouter.ai/api/v1"
KEY = os.environ["OPENROUTER_API_KEY"]
CTX = ssl.create_default_context()
MODELS = [
    ("anthropic/claude-opus-5", 1.0),
    ("google/gemini-3.7-flash", 1.0),
    ("x-ai/grok-4.6", 1.0),
    ("openai/gpt-5.6-sol", None),
]
N = 30


def post(model, temperature):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个助手。"},
            {"role": "user", "content": "回复两个字：收到。"},
        ],
        "max_tokens": 16,
        "provider": {"require_parameters": True},
        "usage": {"include": True},
    }
    if temperature is not None:
        payload["temperature"] = temperature
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + KEY,
            "User-Agent": "sudo-bench-rate-probe/2",
        },
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120, context=CTX) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        usage = body.get("usage") or {}
        return {"ok": True, "status": 200, "latency_s": round(time.monotonic() - t0, 3),
                "cost_usd": usage.get("cost")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        headers = {}
        try:
            meta = (json.loads(raw).get("error") or {}).get("metadata") or {}
            headers = meta.get("headers") or {}
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "status": exc.code, "latency_s": round(time.monotonic() - t0, 3),
                "error": raw[:400], "rl_headers": headers}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "error": "{}: {}".format(type(exc).__name__, exc)}


def wave(model, temperature, lock):
    t0 = time.monotonic()
    out = []
    with ThreadPoolExecutor(max_workers=N) as pool:
        for r in pool.map(lambda _i: post(model, temperature), range(N)):
            out.append(r)
    wall = time.monotonic() - t0
    ok = [r for r in out if r["ok"]]
    rl = [r for r in out if r.get("status") == 429]
    limit = key = reset = None
    for r in rl:
        h = r.get("rl_headers") or {}
        limit = limit or h.get("X-RateLimit-Limit")
        reset = reset or h.get("X-RateLimit-Reset")
        m = re.search(r"Rate limit exceeded: ([^.\"]+)", r.get("error") or "")
        if m and not key:
            key = m.group(1).strip()
    summary = {
        "model": model,
        "sent": N,
        "wall_s": round(wall, 2),
        "ok": len(ok),
        "http_429": len(rl),
        "other_fail": len(out) - len(ok) - len(rl),
        "declared_limit": limit,
        "limit_key": key,
        "reset_header": reset,
        "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in out), 6),
        "sample_429_body": (rl[0].get("error") if rl else None),
    }
    with lock:
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=len(MODELS)) as pool:
        results = list(pool.map(lambda mt: wave(mt[0], mt[1], lock), MODELS))
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump({"probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "burst_size": N,
                   "waves": results}, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
