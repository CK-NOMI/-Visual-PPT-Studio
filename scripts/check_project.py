"""Independent QA module command: python scripts/check_project.py PROJECT_ID"""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from studio.core import read_project
from studio.quality import audit
if len(sys.argv)!=2:raise SystemExit('用法：python scripts/check_project.py <项目ID>')
result=audit(read_project(sys.argv[1]))
print(json.dumps(result,ensure_ascii=False,indent=2))
sys.exit(0 if result['ok'] else 1)
