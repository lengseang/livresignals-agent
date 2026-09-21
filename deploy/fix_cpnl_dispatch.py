"""FIX: move cpnl: dispatch to the top of _handle_callback_query.

Problem: our previous patch put the cpnl: branch inside
_handle_model_picker_callback (only reached for mp:/mpg:/mm:/... prefixes).
cpnl:pnl / cpnl:acc / cpnl:refresh_pnl never matched -> buttons did nothing.

Fix:
  1. Remove the dead cpnl: block from _handle_model_picker_callback
  2. Insert a cpnl: dispatch right after query data extraction in
     _handle_callback_query, before the model-picker branch.
"""
from pathlib import Path

path = Path("/home/ubuntu/hermes-venv/lib/python3.12/site-packages/plugins/platforms/telegram/adapter.py")
src = path.read_text(encoding="utf-8")

# 1. Remove the dead block inside _handle_model_picker_callback.
dead_start = '        elif data.startswith("cpnl:"):\n'
dead_end = '            except Exception as exc:\n                logger.error("Trading callback error: %s", exc)\n'
i = src.find(dead_start)
if i == -1:
    print("dead block not found (already removed?)")
else:
    j = src.find(dead_end, i)
    assert j != -1, "dead block end not found"
    j = src.find("\n\n", j)
    src = src[:i] + src[j + 2:]
    print("removed dead cpnl block from model-picker handler")

# 2. Insert dispatch at the top of _handle_callback_query.
anchor = """        query_user_name = getattr(query.from_user, "first_name", None)

        # --- Model picker callbacks ---"""
insert = """        query_user_name = getattr(query.from_user, "first_name", None)

        # --- Trading bot callbacks (cpnl:action) ---
        # Trading Agent Office 2026-08-12 — button actions for the Trading
        # Signals topics. Lives at the TOP of the dispatcher so it runs for
        # any callback regardless of prefix.
        if data.startswith("cpnl:"):
            action = data.split(":", 1)[1]
            await query.answer()  # acknowledge the tap fast (stops spinner)
            try:
                import subprocess
                cwd = "/opt/trading-agent"
                py = "/opt/trading-agent/venv/bin/python"
                if action in ("pnl", "refresh_pnl"):
                    subprocess.Popen([py, "scripts/broadcast.py", "pnl"],
                                     cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    await query.answer(text="🔄 Refreshing PnL…" if action == "refresh_pnl" else "💵 Sending PnL card…")
                elif action == "acc":
                    subprocess.Popen([py, "scripts/broadcast.py", "acc"],
                                     cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    await query.answer(text="📊 Building accuracy card…")
                elif action == "params":
                    subprocess.Popen([py, "scripts/broadcast.py", "params"],
                                     cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    await query.answer(text="🧠 Building adaptive params card…")
            except Exception as exc:  # noqa: BLE001
                logger.error("Trading callback error: %s", exc)
            return

        # --- Model picker callbacks ---"""
assert anchor in src, "dispatch anchor not found"
src = src.replace(anchor, insert, 1)

path.write_text(src, encoding="utf-8")
print("PATCHED: cpnl dispatch now at top of _handle_callback_query")