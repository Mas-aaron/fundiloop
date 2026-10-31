"""Tolerant latency stats for backend/data/logs/model_calls.jsonl (read-only)."""
import json
import statistics as s
import sys
from collections import defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "backend/data/logs/model_calls.jsonl"
rows, bad = [], 0
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError:
        bad += 1
print(f"rows={len(rows)} bad={bad}")


def elapsed(r):
    for k in ("latency_ms", "elapsed_ms"):
        if r.get(k) is not None:
            try:
                return float(r[k]) / 1000.0
            except (TypeError, ValueError):
                pass
    for k in ("elapsed_s", "duration_s", "latency_s", "seconds"):
        if r.get(k) is not None:
            try:
                return float(r[k])
            except (TypeError, ValueError):
                pass
    return 0.0


groups = defaultdict(list)
for r in rows:
    groups[str(r.get("model", "?"))].append(elapsed(r))
for model, vals in sorted(groups.items()):
    vals.sort()
    n = len(vals)
    p50 = s.median(vals) if vals else 0
    p90 = vals[min(n - 1, int(n * 0.9))] if vals else 0
    print(f"{model} n={n} p50={p50:.1f}s p90={p90:.1f}s max={max(vals) if vals else 0:.0f}s")
slow = [r for r in rows if elapsed(r) > 30]
print(f"calls_over_30s={len(slow)}")
by_model = defaultdict(int)
for r in slow:
    by_model[str(r.get("model", "?"))] += 1
print("slow_by_model=" + json.dumps(dict(by_model)))
print("--- newest 10 ---")
for r in rows[-10:]:
    keys = {k: r.get(k) for k in ("model", "op", "endpoint", "flow", "step") if k in r}
    print(f"{elapsed(r):7.1f}s {json.dumps(keys)}")
