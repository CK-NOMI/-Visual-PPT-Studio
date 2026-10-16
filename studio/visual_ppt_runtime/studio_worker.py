"""Trusted-only subprocess worker; not a model-exposed arbitrary command runner."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--data',required=True);p.add_argument('--project',required=True)
    p.add_argument('--slide',required=True);p.add_argument('--out',required=True)
    args=p.parse_args()
    # PPT_STUDIO_DATA must already be set by the trusted adapter *before* importing studio.core.
    os.environ['PPT_STUDIO_DATA']=str(Path(args.data).resolve())
    from studio import core
    from visual_ppt_runtime.export_bridge import build_with_extensions
    from visual_ppt_runtime.scene import signature
    import pymupdf
    project=core.read_project(args.project)
    output=Path(args.out).resolve();output.mkdir(parents=True,exist_ok=True)
    pptx=output/'candidate.pptx';build_with_extensions(project,pptx,'editable')
    office=shutil.which('soffice') or shutil.which('libreoffice')
    if not office:raise RuntimeError('LibreOffice executable missing (soffice/libreoffice)')
    with tempfile.TemporaryDirectory(prefix='lo-profile-') as tmp:
        cmd=[office,'-env:UserInstallation=file://'+tmp+'/profile','--headless','--convert-to','pdf',
             '--outdir',str(output),str(pptx)]
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=65)
        if r.returncode or not (output/'candidate.pdf').is_file():
            raise RuntimeError('LibreOffice conversion failed: '+(r.stderr or r.stdout)[-500:])
    doc=pymupdf.open(output/'candidate.pdf')
    index=next((i for i,s in enumerate(project['slides']) if s['id']==args.slide),None)
    if index is None:raise ValueError('slide does not exist')
    if index>=len(doc):raise RuntimeError('PDF page count mismatch')
    doc[index].get_pixmap(matrix=pymupdf.Matrix(1.4,1.4),alpha=False).save(output/'slide.png')
    r={'renderer':'LibreOffice','project_signature':signature(project),'pptx_sha256':sha(pptx),
       'image_sha256':sha(output/'slide.png'),'slide_id':args.slide,'revision':project.get('revision')}
    (output/'report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(r,ensure_ascii=False))

if __name__=='__main__':main()
