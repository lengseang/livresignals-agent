"""Fix ctrade: handler to route through the full guard stack (button_trade.py).

Replaces the old handler that called testnet_executor `signal` mode with
force=True (which bypassed scorer/cadence/universe/portfolio guards).
The new handler calls scripts/button_trade.py which uses auto_trader's full
guard stack — a button tap is a nudge, never a force.

Idempotent: detects and rewrites the ctrade block.
"""
from pathlib import Path

path = Path("/home/ubuntu/hermes-venv/lib/python3.12/site-packages/plugins/platforms/telegram/adapter.py")
src = path.read_text(encoding="utf-8")

NEW_BLOCK = '''        # --- Trading auto-trade button (ctrade:symbol) ---
        if data.startswith("ctrade:"):
            sym = data.split(":", 1)[1]
            await query.answer(text="\\u2705 Checking guards…")
            try:
                import subprocess
                cwd = "/opt/trading-agent"
                py = "/opt/trading-agent/venv/bin/python"
                subprocess.Popen(
                    [py, "scripts/button_trade.py", sym],
                    cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                logger.error("ctrade callback error: %s", exc)
            return

'''

import re
# Replace any existing ctrade block (from old version) with the new one.
pattern = re.compile(r"        # --- Trading auto-trade button \(ctrade:symbol\) ---.*?\n        return\n", re.DOTALL)
if pattern.search(src):
    src = pattern.sub(NEW_BLOCK.rstrip() + "\n", src, count=1)
    print("REWROTE existing ctrade handler -> full-guard button_trade.py")
elif "ctrade:" not in src:
    anchor = "        # --- Model picker callbacks ---"
    assert anchor in src, "anchor not found"
    src = src.replace(anchor, NEW_BLOCK + anchor, 1)
    print("ADDED ctrade handler -> full-guard button_trade.py")
else:
    print("ctrade present but not matched — inspect manually")

path.write_text(src, encoding="utf-8")

import ast
ast.parse(src)
print("syntax OK")