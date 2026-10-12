"""Independent PPTX renderer command."""
import sys,json,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from studio.server import render_deck
p=argparse.ArgumentParser(description='导出编号PPT项目')
p.add_argument('id');p.add_argument('--mode',choices=['editable','raster'],default='editable')
a=p.parse_args()
print(json.dumps(render_deck(a.id,a.mode),ensure_ascii=False,indent=2))
