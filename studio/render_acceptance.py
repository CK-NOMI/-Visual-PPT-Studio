"""PPTX provenance and explicitly supplied WPS renders, bound to immutable exports."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import io
import json
import platform
import plistlib
import time
import uuid
import zipfile
from pathlib import Path
from lxml import etree
from PIL import Image
from .core import project_dir, safe_json_write


def wps_environment():
    candidates=[Path('/Applications/wpsoffice.app'),Path.home()/'Applications/wpsoffice.app']
    app=next((p for p in candidates if p.is_dir()),None)
    info={}
    if app:
        info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
    return {'installed':bool(app),'path':str(app) if app else None,
            'version':info.get('CFBundleShortVersionString'), 'bundle_id':info.get('CFBundleIdentifier'),
            'scripting_dictionary':bool(app and list(app.rglob('*.sdef'))),
            'automation':'WPS GUI export; no verified headless/AppleScript API',
            'os':platform.platform()}


def layout_manifest(project, mode='editable'):
    return {'schema_version':2,'project_id':project.get('id'),'project_revision':project.get('revision'),
            'layout_version':project.get('text_layout_version',2),'mode':mode,
            'stage_visual_renderer':'Chromium browser','compatibility_target':'WPS Office for Mac','auxiliary_renderer':'LibreOffice',
            'wps_environment':wps_environment(),'wps_visual_verified':False,'wps_editability_verified':False,
            'font_policy':'explicit installed/bundled faces; no font binaries embedded',
            'pages':[{'slide_id':s['id'],'image_metadata':s.get('image_metadata',{}),
                      'objects':[deepcopy(e) for e in s.get('elements',[])]} for s in project['slides']]}


def embed_manifest(pptx_path, project, mode):
    """Store an XML custom part using the standard OOXML customXml relationship."""
    manifest=layout_manifest(project,mode)
    root=etree.Element('{urn:visual-ppt-studio:layout:v2}manifest')
    root.text=json.dumps(manifest,ensure_ascii=False,separators=(',',':'))
    xml=etree.tostring(root,encoding='UTF-8',xml_declaration=True)
    rel_ns='http://schemas.openxmlformats.org/package/2006/relationships'
    with zipfile.ZipFile(pptx_path) as src:
        entries={name:src.read(name) for name in src.namelist()}
    rel_path='ppt/_rels/presentation.xml.rels'
    rels=etree.fromstring(entries[rel_path])
    etree.SubElement(rels,'{'+rel_ns+'}Relationship',Id='rIdVisualPPTLayout',
                     Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml',
                     Target='../customXml/visualppt-layout.xml')
    entries[rel_path]=etree.tostring(rels,encoding='UTF-8',xml_declaration=True)
    entries['customXml/visualppt-layout.xml']=xml
    with zipfile.ZipFile(pptx_path,'w',zipfile.ZIP_DEFLATED) as dst:
        for name,data in entries.items():dst.writestr(name,data)
    return manifest


def import_wps_render(project, report, data, filename, artifact_sha256, slide_id=None,
                      wps_version='', crop=None):
    """Ingest full-slide PDF/PNG/JPEG; an import is evidence, never automatic acceptance."""
    if artifact_sha256!=report.get('artifact_sha256') or report.get('project_revision')!=project.get('revision'):
        raise ValueError('WPS文件必须绑定当前导出的PPTX哈希和项目版本，请重新导出后导入')
    if len(data)>50*1024*1024:raise ValueError('WPS验收文件最多50MB')
    suffix=Path(filename or '').suffix.lower()
    pages=[]
    if suffix=='.pdf':
        import pymupdf
        with pymupdf.open(stream=data,filetype='pdf') as doc:
            if len(doc)!=len(project['slides']):raise ValueError('WPS PDF页数必须与整套PPTX相同，且每页只包含一张幻灯片')
            for s,page in zip(project['slides'],doc):
                if abs(page.rect.width/page.rect.height-16/9)>.06:
                    raise ValueError('WPS PDF必须导出幻灯片页，不能包含备注、讲义或界面边框')
                pix=page.get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False)
                pages.append((s['id'],Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')))
    elif suffix in ('.png','.jpg','.jpeg','.webp'):
        if slide_id not in {s['id'] for s in project['slides']}:raise ValueError('导入截图必须指定准确的页面编号')
        im=Image.open(io.BytesIO(data)).convert('RGB')
        if crop:
            if len(crop)!=4 or any(not isinstance(x,(int,float)) for x in crop):raise ValueError('截图裁剪框需要 [x,y,w,h] 像素坐标')
            x,y,w,h=crop
            if min(x,y)<0 or min(w,h)<=0 or x+w>im.width or y+h>im.height:raise ValueError('截图裁剪框超出图片')
            im=im.crop((x,y,x+w,y+h))
        if abs(im.width/im.height-16/9)>.06:raise ValueError('请上传或裁剪为仅包含16:9幻灯片的截图')
        pages.append((slide_id,im))
    else:raise ValueError('支持 WPS 导出的 PDF 或 PNG/JPEG/WebP 幻灯片截图')
    import_id=uuid.uuid4().hex[:16]
    folder=project_dir(project['id'])/'build'/'wps_acceptance'/import_id
    folder.mkdir(parents=True)
    (folder/('source'+suffix)).write_bytes(data)
    result={'import_id':import_id,'renderer':'WPS Office (user supplied export)',
            'wps_version':wps_version or 'not supplied','project_revision':project['revision'],
            'artifact_sha256':artifact_sha256,'source_sha256':hashlib.sha256(data).hexdigest(),
            'imported_at':time.time(),'status':'awaiting_manual_review',
            'visual_verified':False,'editability_verified':False,'pages':[]}
    from .reconstruction import compare_render
    for sid,im in pages:
        file=folder/f'{sid}.png';im.save(file)
        comparison=compare_render(project,sid,file,output_dir=folder)
        result['pages'].append({'slide_id':sid,'preview':file.name,'comparison':comparison})
    safe_json_write(folder/'report.json',result)
    return result
