"""Is `hold=30` real, or did I just curve-fit the sweep?

The batch-3 sweep picked hold=30 because it had the best FULL-period
return. That is exactly the trap. Test: run each parameter across the
walk-forward folds and ask "is it positive in MOST folds?" - not "is its
average highest?".
"""
import importlib.util
import statistics

spec = importlib.util.spec_from_file_location(
    "w3", "/opt/trading-agent/research/06-walkforward.py")
w3 = importlib.util.module_from_spec(spec)
import sys
sys.modules["w3"] = w3
spec.loader.exec_module(w3)

bt, bh, n, folds = w3.bt, w3.bh, w3.n, w3.folds
px = w3.px

print("PARAMETER ROBUSTNESS ACROSS WALK-FORWARD FOLDS")
print("(positive in the MOST folds = robust; best average = probably overfit)")
print("=" * 76)
print(f"{'config':30} " + " ".join(f"{'f'+str(i+1):>7}" for i in range(len(folds)))
      + f" {'median':>8} {'+folds':>7}")
print("-" * 76)
for name, kw in [
        ("regime topn3 hold14", {"topn": 3, "hold": 14}),
        ("regime topn3 hold21", {"topn": 3, "hold": 21}),
        ("regime topn3 hold30", {"topn": 3, "hold": 30}),
        ("regime topn3 hold45", {"topn": 3, "hold": 45}),
        ("regime topn5 hold21", {"topn": 5, "hold": 21}),
        ("regime topn3 h21 r0.02", {"topn": 3, "hold": 21, "risk": 0.02}),
        ("regime topn3 h21 sma150", {"topn": 3, "hold": 21, "sma_r": 150}),
        ("regime topn3 h21 sma100", {"topn": 3, "hold": 21, "sma_r": 100}),
]:
    rets = [bt(a, b, "regime", **kw)[0] for a, b in folds]
    med = statistics.median(rets)
    pos = sum(1 for r in rets if r > 0)
    flag = "  <-- ROBUST" if pos >= len(folds) - 1 and med > 0 else ""
    print(f"{name:30} " + " ".join(f"{r:>6.1f}%" for r in rets)
          + f" {med:>7.1f}% {pos:>4}/{len(folds)}{flag}")

print()
bench = [bh("BTC", a, b) for a, b in folds]
print(f"{'BTC buy&hold':30} " + " ".join(f"{r:>6.1f}%" for r in bench)
      + f" {statistics.median(bench):>7.1f}% "
      f"{sum(1 for r in bench if r>0):>4}/{len(folds)}")
eql = [sum(bh(c, a, b) for c in w3.coins) / len(w3.coins) for a, b in folds]
print(f"{'equal-weight basket':30} " + " ".join(f"{r:>6.1f}%" for r in eql)
      + f" {statistics.median(eql):>7.1f}% "
      f"{sum(1 for r in eql if r>0):>4}/{len(folds)}")
