from io import BytesIO
from pathlib import Path
import pytest
from PIL import Image
from studio import core
from studio.quality import audit

def image_bytes(format='PNG'):
    b=BytesIO();Image.new('RGB',(640,360),'white').save(b,format=format);return b.getvalue()

@pytest.fixture(autouse=True)
def isolate(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path)

def test_numbered_project_and_lock_contract():
    p=core.create_project('演示','研究设计',pages=4)
    assert [s['id'] for s in p['slides']]==['S001','S002','S003','S004']
    assert all(s['title']=='' and s['body']=='' for s in p['slides'])
    assert all(e['text']=='' for s in p['slides'] for e in s['elements'])
    with pytest.raises(ValueError,match='锁定'):core.write_image(p,'S001','design',image_bytes(),'x.png')
    p['locked']=True;core.save_project(p)
    r=core.write_image(p,'S001','design',image_bytes(),'chatgpt-output.png')
    assert r['filename']=='S001_design.png'
    assert 'design' in core.find_slide(p,'S001')['images']
    assert core.status(p)['design']==1
    with pytest.raises(ValueError):core.write_image(p,'S005','design',image_bytes(),'x.png')

def test_prompt_archive_and_clean_background_rules():
    p=core.create_project('研发汇报','架构演进',3)
    p['locked']=True;core.save_project(p)
    out=core.build_prompt_zip(p)
    import zipfile
    with zipfile.ZipFile(out) as z:
        assert 'prompts/S001_提示词.txt' in z.namelist()
        assert 'S003_design.png' in z.read('prompts/S003_提示词.txt').decode()
    q=audit(p)
    assert q['counts']['warnings']>=6
    assert q['ok']

def test_source_truth_sync_and_validation():
    p=core.create_project('原始标题','正文',1)
    s=core.update_slide(p,'S001',{'title':'新标题','body':'新正文'})
    assert next(e for e in s['elements'] if e['id']=='title')['text']=='新标题'
    els=s['elements']
    els[0]['text']='模型误识别'
    s=core.update_slide(p,'S001',{'elements':els})
    assert s['elements'][0]['text']=='新标题'
    els[0]['box']=[-0.2,0.1,0.3,0.2]
    core.update_slide(p,'S001',{'elements':els})
    assert audit(p)['counts']['errors']>=1


def test_strict_upload_file_role():
    p=core.create_project('A','B',1)
    p['locked']=True;core.save_project(p)
    with pytest.raises(ValueError):core.write_image(p,'S001','garbage',image_bytes(),'x')
    with pytest.raises(ValueError):core.write_image(p,'S001','clean',b'not-image','x')
    assert core.FILENAME_RE.fullmatch('S001_asset_01.png')
    assert core.FILENAME_RE.fullmatch('no-number.png') is None
