"""Add ctrade: (auto-trade button) to the gateway callback dispatcher.

Idempotent: if ctrade already present, prints ALREADY PATCHED and exits.
Inserts the ctrade: handler right after the existing cpnl: block, or if
cpnl: is absent, after the query data extraction anchor.
"""
from pathlib import Path

path = Path("/home/ubuntu/hermes-venv/lib/python3.12/site-packages/plugins/platforms/telegram/adapter.py")
src = path.read_text(encoding="utf-8")

CTRADE_BLOCK = '''        # --- Trading auto-trade button (ctrade:symbol) ---
        if data.startswith("ctrade:"):
            sym = data.split(":", 1)[1]
            await query.answer(text="\\u2705 Executing trade…")
            try:
                import subprocess
                cwd = "/opt/trading-agent"
                py = "/opt/trading-agent/venv/bin/python"
                subprocess.Popen(
                    [py, "-m", "trading.allocation.testnet_executor", "signal", "50", sym],
                    cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                logger.error("ctrade callback error: %s", exc)
            return

'''

if "ctrade:" in src:
    print("ALREADY PATCHED (ctrade present)")
else:
    # Insert right before the "# --- Model picker callbacks ---" anchor.
    anchor = "        # --- Model picker callbacks ---"
    assert anchor in src, "model-picker anchor not found"
    src = src.replace(anchor, CTRADE_BLOCK + anchor, 1)
    path.write_text(src, encoding="utf-8")
    print("PATCHED: ctrade handler added")

# Verify syntax
import ast
ast.parse(src)
print("syntax OK")
