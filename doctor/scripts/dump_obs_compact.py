"""Dump only obs names + inputs (compact) for a trace."""
from __future__ import annotations
import os, sys, json
from langfuse import Langfuse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import settings  # noqa: E402

lf = Langfuse(secret_key=settings.langfuse_secret_key, public_key=settings.langfuse_public_key, host=settings.langfuse_host)
trace = lf.fetch_trace(sys.argv[1]).data
flat = []
for obs in (trace.observations or []):
    flat.append(obs)
    for c in (getattr(obs, "children", None) or []):
        flat.append(c)

for i, obs in enumerate(flat, 1):
    nm = getattr(obs, "name", "") or ""
    t = getattr(obs, "type", "?") or "?"
    inp = getattr(obs, "input", None) or {}
    outp = getattr(obs, "output", None) or {}
    args = inp.get("args") if isinstance(inp, dict) else None
    # extract file_path or query or sql
    hint = ""
    if isinstance(args, dict):
        hint = json.dumps({k: args[k] for k in ("file_path","start_line","end_line","query","sql","source","analysis") if k in args}, ensure_ascii=False)
    # for llm_call, show output content snippet
    if t == "GENERATION":
        c = (outp.get("content") if isinstance(outp, dict) else "") or ""
        hint = (c[:160] + ("..." if len(c) > 160 else "")).replace("\n", " ")
    else:
        # show first 240 chars of result
        r = outp.get("result") if isinstance(outp, dict) else str(outp)
        r = (r or "")
        # try to extract just file path or first meaningful line
        if "文件]" in r or "[文件" in r or "file_path" in r:
            hint += "  -> " + r[:200].replace("\n"," ")
        elif "match_type" in r or "ripgrep" in r:
            # extract matched file_paths
            try:
                obj = json.loads(r)
                if isinstance(obj, list):
                    files = sorted({x.get("file_path","") + ":" + str(x.get("line_number","")) for x in obj if isinstance(x, dict)})
                    hint += "  -> " + " | ".join(files)
                elif isinstance(obj, dict) and obj.get("results") == []:
                    hint += "  -> NO_MATCH"
            except Exception:
                hint += "  -> " + r[:120].replace("\n"," ")
        else:
            hint += "  -> " + r[:120].replace("\n"," ")
    print(f"{i:>3} {t[:4]:<4} {nm:<32} {hint}")
