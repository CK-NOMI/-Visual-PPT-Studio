"""User-provided real dense infographic benchmark, no external APIs.

Runs a safe reconstruction baseline, then selectively promotes ONE short
editable phrase. The rest of the infographic remains a faithful raster layer.
Use this as the reproducible diagnostic benchmark rather than an editability claim.
"""
from __future__ import annotations
import json
from pathlib import Path
from studio import core, reconstruction as rc, server

SRC=Path(__file__).with_name('S001_transformer_reference.png')

def main():
    if not SRC.exists():raise SystemExit('缺少 samples/S001_transformer_reference.png')
    project=core.create_project('Transformer结构详解（真实信息图调试）',
        '一页高密度 Transformer 结构图。先保证视觉原样，再逐块提升可编辑文字。',1,'editorial')
    project['locked']=True;core.save_project(project)
    core.write_image(project,'S001','design',SRC.read_bytes(),SRC.name)
    rc.initialize_raster_first(project,'S001')
    # Preserve the untouched visual baseline via the source image; avoid an
    # unnecessary first Office render before the selective editable pass.
    baseline={'pdf':None}
    # Top right headline: chosen manually after looking at supplied bitmap.
    # One region only: no OCR guessing, no content rewrite, no bulk erasing.
    region={'text':'更强的表示能力','role':'custom','editable':True,
            'box':[1488/1672,25/941,160/1672,29/941], 'confidence':1.0}
    rc.persist_regions(project,'S001',[region])
    trial=rc.generate_clean_candidate(project,'S001',[region],mask_mode='ink',method='telea',padding=2,threshold=36)
    rc.accept_clean(project,'S001')
    obj={'id':'ocr_001','kind':'text','text':region['text'],'box':region['box'],
         'style':{'size':11.5,'color':'0B2B5D','font':'Noto Sans CJK SC'}}
    core.update_slide(project,'S001',{'elements':[obj]})
    report=server.render_deck(project['id'],'editable')
    compare=rc.compare_render(project,'S001',core.project_dir(project['id'])/'build'/'editable_S001_preview.png')
    output={'project_id':project['id'],'workspace':str(core.project_dir(project['id'])),
            'baseline_pdf':baseline['pdf'],'native_text':report['native_text'],
            'renderer':report['builder'],'coverage':rc.reconstruction_coverage(project,'S001'),
            'inpainting':trial['report'],'render_comparison':compare,
            'notice':'仅右上角一处被提升为原生文字，其余密集信息图仍是位图。'}
    out=core.project_dir(project['id'])/'build'/'transformer_benchmark.json'
    out.write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps(output,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
