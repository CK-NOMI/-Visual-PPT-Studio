"""Real LibreOffice adapter for existing Studio; isolation in subprocess and temp workspace.

Does not provide a synthetic screenshot as evidence of visual improvement.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from .errors import RendererUnavailable,ValidationError
from .scene import signature
from .tools import RenderOutcome

class StudioLibreOfficeRenderer:
    """Uses existing studio.pptx_native.build in a separate isolated workspace.

    Root points at existing visual_ppt_studio checkout, source_dir is its current
    project workspace/<ID>, assets are copied to a temporary workspace. Requires
    python-pptx, pymupdf and soffice on the integration machine.
    """
    def __init__(self,studio_root:Path,source_project_dir:Path,output_dir:Path,*,python_executable:str|None=None):
        self.studio_root=Path(studio_root).resolve()
        self.source_dir=Path(source_project_dir).resolve()
        self.output_dir=Path(output_dir).resolve()
        self.python=python_executable or sys.executable
        if not (self.studio_root/'studio'/'pptx_native.py').is_file():
            raise RendererUnavailable('studio_root missing studio/pptx_native.py')
        if not (self.source_dir/'images').is_dir():raise RendererUnavailable('source project images directory missing')
        self.output_dir.mkdir(parents=True,exist_ok=True)
        self.counter=0

    def __call__(self,project:dict,sid:str)->RenderOutcome:
        pid=project.get('id')
        if not isinstance(pid,str) or len(pid)!=12 or any(c not in '0123456789abcdef' for c in pid):
            raise ValidationError('Studio project ID must be 12 lowercase hex chars')
        if not any(p.get('id')==sid for p in project['slides']):raise ValidationError('slide not in project')
        self.counter+=1
        tag=f'round-{self.counter:03d}-{signature(project)[:12]}'
        final=self.output_dir/tag
        if final.exists():raise RendererUnavailable('render output collision')
        final.mkdir()
        # Fresh temp source workspace, never imports a live project data folder.
        with tempfile.TemporaryDirectory(prefix='visual-edit-') as work:
            data=Path(work)/'workspace'
            folder=data/pid
            folder.mkdir(parents=True)
            shutil.copytree(self.source_dir/'images',folder/'images')
            (folder/'build').mkdir()
            (folder/'project.json').write_text(json.dumps(project,ensure_ascii=False,indent=2),encoding='utf-8')
            env=os.environ.copy();env['PPT_STUDIO_DATA']=str(data)
            env['PYTHONPATH']=str(self.studio_root)+os.pathsep+str(Path(__file__).resolve().parent.parent)+os.pathsep+env.get('PYTHONPATH','')
            # Reuse Studio's exact LibreOffice font setup. A headless LO render
            # without this config can turn valid CJK runs into tofu glyphs and
            # would therefore be false visual feedback to the model.
            office=shutil.which('soffice') or shutil.which('libreoffice')
            if office:
                try:
                    from studio.server import office_font_environment
                    env.update(office_font_environment(office,str(final/'fontconfig')))
                except Exception as exc:
                    raise RendererUnavailable('Studio LibreOffice font setup failed: '+type(exc).__name__) from exc
            cmd=[self.python,'-m','visual_ppt_runtime.studio_worker','--data',str(data),
                 '--project',pid,'--slide',sid,'--out',str(final)]
            try:p=subprocess.run(cmd,cwd=self.studio_root,env=env,capture_output=True,text=True,timeout=150)
            except subprocess.TimeoutExpired:raise RendererUnavailable('LibreOffice renderer timed out')
            if p.returncode:
                raise RendererUnavailable('Studio renderer failed: '+(p.stderr or p.stdout)[-900:])
        report_path=final/'report.json'
        if not report_path.exists():raise RendererUnavailable('renderer completed without report')
        r=json.loads(report_path.read_text(encoding='utf-8'))
        if r.get('project_signature')!=signature(project):raise RendererUnavailable('mismatched renderer project signature')
        return RenderOutcome(str(final/'slide.png'),r['renderer'],r['project_signature'],r['image_sha256'],
                             str(final/'candidate.pptx'),r['pptx_sha256'],project.get('revision'))
