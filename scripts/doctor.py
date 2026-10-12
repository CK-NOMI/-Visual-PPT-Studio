"""Offline environment diagnostics; no external requests."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from studio.server import health
print(json.dumps(health(),ensure_ascii=False,indent=2))
