"""One command: real browser, object crops, exact PPTX and multi-page workflow.

python scripts/qa_runner.py --browser --slides S001,S002
Runs in an isolated copy of the selected local workspace, never alters its source.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
if importlib.util.find_spec('playwright') is None:
    venv=ROOT/'.venv/bin/python'
    if venv.is_file() and Path(sys.executable).resolve()!=venv.resolve():
        os.execv(str(venv),[str(venv),__file__,*sys.argv[1:]])

DOM_OBSERVATION=r'''() => {
 const snapshot=window.visualPPTStudioQA.snapshot(),stage=document.querySelector('#stage'),s=stage.getBoundingClientRect();
 const rect=r=>({x:r.x-s.x,y:r.y-s.y,width:r.width,height:r.height});
 const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');
 const objects=[...stage.querySelectorAll('[data-el]')].filter(n=>snapshot.elements.some(e=>e.id===n.dataset.el&&['text','formula'].includes(e.kind)&&e.text.trim())).map(node=>{
  const e=snapshot.elements.find(e=>e.id===node.dataset.el),style=getComputedStyle(node),line=node.querySelector('.glyph-line')||node;
  const walker=document.createTreeWalker(line,NodeFilter.SHOW_TEXT);let text='',textNodes=[],child;while(child=walker.nextNode()){text+=child.textContent;textNodes.push(child)}
  const fragments=textNodes.map(n=>{const r=document.createRange();r.selectNodeContents(n);return {text:n.textContent,rects:[...r.getClientRects()].map(rect),font:getComputedStyle(n.parentElement||node)}}),ranges=fragments.flatMap(f=>f.rects);
  const marker=node.querySelector('.baseline-marker'),baseline=marker?marker.getBoundingClientRect().top-s.top:node.getBoundingClientRect().bottom-s.top;
  const n=node.getBoundingClientRect(),inkBoxes=[],trackingValues=[];let canvasAdvance=0;
  for(const f of fragments){if(!f.text.trim()||!f.rects.length)continue;ctx.font=`${f.font.fontStyle} ${f.font.fontWeight} ${f.font.fontSize} ${f.font.fontFamily}`;ctx.fontKerning='normal';const m=ctx.measureText(f.text),x=Math.min(...f.rects.map(r=>r.x)),tracking=parseFloat(f.font.letterSpacing)||0,w=m.actualBoundingBoxLeft+m.actualBoundingBoxRight+Math.max(0,Array.from(f.text).length-1)*tracking,h=m.actualBoundingBoxAscent+m.actualBoundingBoxDescent;canvasAdvance+=m.width;trackingValues.push(tracking);if(w>0&&h>0)inkBoxes.push({x:x-m.actualBoundingBoxLeft,y:baseline-m.actualBoundingBoxAscent,width:w,height:h})}
  if(!inkBoxes.length)return null;
  const ink={x:Math.min(...inkBoxes.map(x=>x.x)),y:Math.min(...inkBoxes.map(x=>x.y)),width:Math.max(...inkBoxes.map(x=>x.x+x.width))-Math.min(...inkBoxes.map(x=>x.x)),height:Math.max(...inkBoxes.map(x=>x.y+x.height))-Math.min(...inkBoxes.map(x=>x.y))};
  const font=e.text_layout?.font,status=font?snapshot.fontStatus[`${font.file_identity}:${font.requested_weight}`]:null;
  return {id:e.id,text:e.kind==='text'?text:e.text,kind:e.kind,box:[(n.x-s.x)/s.width,(n.y-s.y)/s.height,n.width/s.width,n.height/s.height],range_rects:ranges,ink_box:e.kind==='text'?ink:null,baseline_px:baseline,font_size_px:parseFloat(style.fontSize),font_family:style.fontFamily,font_weight:style.fontWeight,font_color:style.color,font_status:status?.status||'legacy_unregistered',font_fallback:status?.fallback||false,canvas_advance_px:canvasAdvance,letter_spacing_px:trackingValues[0]||0,margins_px:[style.paddingLeft,style.paddingTop,style.paddingRight,style.paddingBottom],white_space:style.whiteSpace,overflow:style.overflow};
 });
 return {stage:{width:s.width,height:s.height},objects,browser:navigator.userAgent,font_set_status:document.fonts.status};
}'''


def wait_server(url,process):
    import httpx
    for _ in range(160):
        if process.poll() is not None:raise RuntimeError('Local service failed; see server.log')
        try:
            if httpx.get(url+'/api/projects',timeout=1).status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(.2)
    raise RuntimeError('Local service startup timed out')


def select_page(page,sid):
    page.wait_for_function('()=>window.visualPPTStudioQA?.snapshot().project&&!window.visualPPTStudioQA.snapshot().projectLoading')
    page.locator('[data-tab="plan"]').click()
    page.locator(f'.plan-entry[data-sid="{sid}"]').click()
    page.wait_for_function('(sid)=>{const s=window.visualPPTStudioQA.snapshot();return s.sid===sid&&!s.projectLoading&&s.elements.length===s.project.slides.find(p=>p.id===sid).elements.length}',arg=sid)
    details=page.locator('.nav-advanced')
    if details.count() and details.get_attribute('open') is None:details.locator('summary').click()
    page.locator('[data-tab="editor"]').click()
    page.wait_for_function('()=>document.querySelector("#stage").clientWidth>0')
    page.evaluate('async()=>{await document.fonts.ready;window.visualPPTStudioQA.alignBaselines();await new Promise(requestAnimationFrame)}')


def disable_auto_calibration(page):
    """Turn off optional calibration controls hidden in the advanced panel."""
    details=page.locator('.qa-advanced')
    if details.count() and details.get_attribute('open') is None:
        details.locator('summary').first.click()
    page.locator('#autoSupervise').uncheck()
    page.locator('#autoApplyCalibration').uncheck()


def choose_chromium():
    # Reuse an installed Playwright binary if its revision differs from this
    # Python package; no browser is installed or downloaded during a QA run.
    cache=Path.home()/'Library/Caches/ms-playwright'
    candidates=sorted(cache.glob('chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell'))
    return str(candidates[-1]) if candidates else None


def workflow_fixture(page,url,data_root,source_project):
    """Exercise upload/rebuild/save/refresh with explicit deterministic provider fixtures."""
    page.route('**/review-image-pair',lambda route:route.fulfill(json={
        'score':98,'usable_as_clean':True,'source_pair_current':True,'repairable_clean':False,
        'residual_text':[],'layout_changes':[],'nontext_damage':[],'notice':'QA semantic-provider fixture'}))
    page.route('**/analyze-structure',lambda route:route.fulfill(json={'objects':[],'notice':'QA semantic-provider fixture'}))
    page.route('**/ocr?backend=auto',lambda route:route.fulfill(json={'regions':[
        {'id':'fixture','text':'中文 English 中文','box':[.12,.25,.6,.045],
         'confidence':.99,'style':{'size':18,'font':'Arial','eastAsianFont':'Microsoft YaHei','color':'17243A'}}]}))
    page.locator('#newProject').click()
    page.locator('#newTitle').fill('QA 两页工作流隔离样本')
    page.locator('#newTopic').fill('验证上传、自动重建、保存、刷新和导出')
    page.locator('#newPages').fill('2')
    with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/api/projects')) as event:
        page.locator('#submitNew').click()
    p=event.value.json();pid=p['id']
    page.wait_for_function('(id)=>{const s=window.visualPPTStudioQA.snapshot();return s.project?.id===id&&!s.projectLoading}',arg=pid)
    with page.expect_response(lambda r:r.url.endswith('/lock')):
        page.locator('#lockPlan').click()
    page.on('dialog',lambda d:d.accept())
    for sid in ('S001','S002'):
        page.locator('[data-tab="upload"]').click()
        page.locator('#slotSlide').select_option(sid)
        for role in ('design','clean'):
            page.locator('#slotRole').select_option(role)
            source=data_root/source_project['id']/'images'/source_project['slides'][int(sid[1:])-1]['images'][role]
            page.locator('#slotFile').set_input_files(source)
            with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/upload')):
                page.locator('#uploadOne').click()
            page.wait_for_function('()=>!document.querySelector("#notice").classList.contains("error")')
        page.locator('#autoRebuildPage').click()
        page.wait_for_function('()=>!document.querySelector("#confirmAutoRebuild").disabled',timeout=45000)
        page.locator('#confirmAutoRebuild').click()
        page.wait_for_function('(sid)=>window.visualPPTStudioQA.snapshot().reconstructionPages.some(p=>p.slide_id===sid&&p.confirmed)',arg=sid)
    page.reload()
    page.wait_for_function('(id)=>{const s=window.visualPPTStudioQA.snapshot();return s.project?.id===id&&!s.projectLoading}',arg=pid)
    snapshot=page.evaluate('()=>window.visualPPTStudioQA.snapshot()')
    confirmed=all(p['confirmed'] for p in snapshot['reconstructionPages']) and len(snapshot['reconstructionPages'])==2
    page.locator('[data-tab="qa"]').click()
    disable_auto_calibration(page)
    page.locator('[data-tab="upload"]').click()
    with page.expect_response(lambda r:'/export?' in r.url and r.request.method=='POST',timeout=60000) as event:
        page.locator('#finishReconstruction').click()
    export=event.value.json()
    return {'pass':confirmed and export.get('native_text',{}).get('ok') is True and export.get('saved_layout_check',{}).get('ok') is True,
            'project_id':pid,'confirmed_after_refresh':confirmed,'export':export.get('filename'),
            'provider_mode':'explicit mocked semantic/AI and OCR responses; validates workflow, not OCR/model accuracy'}



def workflow_manual_drawer(page,url,pid,folder):
    """Exercise the production recovery UI; leaves the source project untouched."""
    before=page.request.get(url+f'/api/projects/{pid}').json()
    page.locator('[data-tab="upload"]').click()
    sid=page.evaluate('()=>window.visualPPTStudioQA.snapshot().sid')
    page.locator('#openManualRebuild').click()
    page.wait_for_function('()=>document.querySelector("#manualDrawer").open&&document.querySelectorAll("[data-labrow]").length>0')
    field=page.locator('[data-labrow="0"] .lab-size')
    size=max(6,float(field.input_value())-1);field.fill(str(size))
    page.locator('#manualPreview').click()
    page.wait_for_function('()=>document.querySelector("#manualDrawerMessage").textContent.includes("预览已更新")')
    page.locator('#manualDrawer').screenshot(path=folder/'manual_drawer.png')
    with page.expect_response(lambda r:r.request.method=='PUT' and r.url.endswith('/'+sid)) as event:
        page.locator('#labApplyTexts').click()
    if event.value.status!=200:raise RuntimeError('Manual drawer save failed: '+event.value.text())
    page.wait_for_function('()=>!document.querySelector("#manualDrawer").open&&!window.visualPPTStudioQA.snapshot().projectLoading')
    after=page.request.get(url+f'/api/projects/{pid}').json()
    old=next(s for s in before['slides'] if s['id']==sid);new=next(s for s in after['slides'] if s['id']==sid)
    other=all(s==next(a for a in after['slides'] if a['id']==s['id']) for s in before['slides'] if s['id']!=sid)
    page.reload();page.wait_for_function('()=>window.visualPPTStudioQA?.snapshot().project&&!window.visualPPTStudioQA.snapshot().projectLoading')
    reopened=page.request.get(url+f'/api/projects/{pid}').json()
    stable=next(s for s in reopened['slides'] if s['id']==sid)['elements']==new['elements']
    saved=any(e.get('style',{}).get('size')==size for e in new['elements'] if e['kind']=='text')
    return {'pass':other and stable and saved and old['images']==new['images'],
            'other_page_unchanged':other,'saved_after_refresh':stable,'uploaded_images_unchanged':old['images']==new['images']}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser',action='store_true',help='Run the real Chromium UI (default)')
    parser.add_argument('--slides',default='S001,S002')
    parser.add_argument('--project',default=None)
    parser.add_argument('--output',default=None)
    parser.add_argument('--skip-workflow',action='store_true')
    parser.add_argument('--preview-only',action='store_true',help='仅在真实 Chromium 中渲染指定页面，不运行 PPTX/全量 QA')
    parser.add_argument('--strict-visual',action='store_true',help='Fail also on review-only visual/recognized-content differences')
    args=parser.parse_args()
    import re
    slides=args.slides.split(',')
    if not all(re.fullmatch(r'S\d{3}',sid) for sid in slides):parser.error('slides must be S001,S002,...')
    from studio import core
    from studio.text_layout import layout_project
    from studio.quality import check_native_text,check_saved_layout
    from studio.browser_qa import assess_page,write_html
    from studio import reconstruction as rc
    source_data=core.DATA
    candidates=[core.read_project(p['id']) for p in core.list_projects()]
    project=next((p for p in candidates if (not args.project or p['id']==args.project) and
                  all(any(s['id']==sid and s['images'].get('design') and s['images'].get('clean') for s in p['slides']) for sid in slides)),None)
    if not project:parser.error('No local project has design/clean for requested slides; use --project ID')
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    folder=Path(args.output).resolve() if args.output else ROOT/'workspace'/project['id']/'build'/'browser_qa'/stamp
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'original_project.json').write_text(json.dumps(project,ensure_ascii=False,indent=2))
    candidate,layout=layout_project(project)
    data_root=folder/'workspace';clone=data_root/project['id'];clone.mkdir(parents=True)
    shutil.copytree(source_data/project['id']/'images',clone/'images')
    (clone/'build').mkdir()
    (clone/'project.json').write_text(json.dumps(candidate,ensure_ascii=False,indent=2))
    core.DATA=data_root
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    url=f'http://127.0.0.1:{port}'
    env=os.environ.copy();env.update(PPT_STUDIO_DATA=str(data_root),PPT_PREVIEW_DISABLE='1')
    log=(folder/'server.log').open('w')
    server=subprocess.Popen([sys.executable,'-m','uvicorn','studio.server:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
    report={'source_project_id':project['id'],'source_revision':project['revision'],'renderer':'Chromium browser',
            'pages':[],'workflow':{},'wps_automation':False,'office_compatibility_verified':False,'console_errors':[]}
    try:
        wait_server(url,server)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,executable_path=choose_chromium())
            from importlib.metadata import version
            report['browser_environment']={'browser_version':browser.version,'executable':choose_chromium(),
                'dependencies':{name:version(name) for name in ('playwright','Pillow','fonttools','uharfbuzz','python-pptx')}}
            context=browser.new_context(viewport={'width':1920,'height':1080},device_scale_factor=2)
            page=context.new_page();page.on('pageerror',lambda error:report['console_errors'].append(str(error)))
            page.goto(url)
            page.wait_for_function('()=>window.visualPPTStudioQA?.snapshot().project&&!window.visualPPTStudioQA.snapshot().projectLoading')
            page.locator(f'[data-pid="{project["id"]}"]').click()
            page.add_style_tag(content='.stage-el.selected{outline:none!important;box-shadow:none!important}')
            snapshots={}
            for sid in slides:
                print('Browser capture',sid,flush=True)
                select_page(page,sid)
                if args.preview_only:
                    # Iteration preview only needs the selected stage. Skip the
                    # full-page capture, DOM calibration, OCR/layout comparison,
                    # and export checks; those run once in final validation.
                    page.locator('#stage').screenshot(path=folder/f'{sid}_stage.png')
                    report['pages'].append({'slide_id':sid,'stage_screenshot':f'{sid}_stage.png',
                        'preview_only':True})
                    report['preview_only']=True
                    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
                    browser.close()
                    return
                before=page.evaluate('()=>window.visualPPTStudioQA.snapshot().elements')
                expected=next(s for s in candidate['slides'] if s['id']==sid)['elements']
                if not before or [e.get('text') for e in before]!=[e.get('text') for e in expected]:
                    raise RuntimeError(sid+' contents differ from original candidate before save')
                with page.expect_response(lambda r:r.request.method=='PUT' and r.url.endswith('/'+sid)):
                    page.locator('#saveElements').click()
                page.wait_for_function('(sid)=>{const s=window.visualPPTStudioQA.snapshot();return s.sid===sid&&!s.projectLoading&&s.elements.length>0}',arg=sid)
                page.evaluate('async()=>{await document.fonts.ready;window.visualPPTStudioQA.alignBaselines();await new Promise(requestAnimationFrame)}')
                snapshot=page.evaluate('()=>window.visualPPTStudioQA.snapshot()');snapshots[sid]=snapshot['elements']
                observation_before=page.evaluate('()=>window.visualPPTStudioQA.snapshot()')
                calibration_observation=page.evaluate('async sid=>await window.visualPPTStudioQA.captureCalibrationLayoutObservation(sid)',sid)
                observation_after=page.evaluate('()=>window.visualPPTStudioQA.snapshot()')
                text_count=sum(1 for e in expected if e.get('kind')=='text' and str(e.get('text','')).strip())
                observation_pass=bool(calibration_observation and
                    calibration_observation.get('project_id')==snapshot['project']['id'] and
                    calibration_observation.get('project_revision')==snapshot['project']['revision'] and
                    calibration_observation.get('slide_id')==sid and
                    len(calibration_observation.get('objects',[]))==text_count and
                    observation_before==observation_after)
                report.setdefault('calibration_observations',{})[sid]={
                    'pass':observation_pass,'project_revision':calibration_observation.get('project_revision') if calibration_observation else None,
                    'objects':len(calibration_observation.get('objects',[])) if calibration_observation else 0,
                    'text_objects_expected':text_count,'ui_state_restored':observation_before==observation_after}
                if not observation_pass:raise RuntimeError(sid+' Chromium calibration observation is stale, partial, or mutated UI state')
                (folder/f'{sid}_calibration_dom.json').write_text(json.dumps(calibration_observation,ensure_ascii=False,indent=2))
                page.screenshot(path=folder/f'{sid}_page.png',full_page=True)
                page.locator('#stage').screenshot(path=folder/f'{sid}_stage.png')
                obs=page.evaluate(DOM_OBSERVATION)
                cdp=context.new_cdp_session(page);cdp.send('DOM.enable');cdp.send('CSS.enable')
                root=cdp.send('DOM.getDocument')['root']['nodeId']
                for obj in obs['objects']:
                    node=cdp.send('DOM.querySelector',{'nodeId':root,'selector':f'[data-el="{obj["id"]}"]'})['nodeId']
                    obj['platform_fonts']=cdp.send('CSS.getPlatformFontsForNode',{'nodeId':node})['fonts'] if node else []
                cdp.detach()
                (folder/f'{sid}_dom.json').write_text(json.dumps(obs,ensure_ascii=False,indent=2))
                current=page.request.get(url+f'/api/projects/{project["id"]}').json()
                source_slide=next(s for s in current['slides'] if s['id']==sid)
                comparison=rc.compare_render(current,sid,folder/f'{sid}_stage.png',output_dir=folder)
                result=assess_page(current,source_slide,obs,folder/f'{sid}_stage.png',folder,comparison)
                report['pages'].append(result)
                print(sid,'layout failed:',result['failed_objects'],'visual review:',result['review_objects'],flush=True)
            # Real navigation, reload and reopening must preserve both saved pages.
            page.reload();page.wait_for_function('()=>window.visualPPTStudioQA?.snapshot().project&&!window.visualPPTStudioQA.snapshot().projectLoading')
            reopened={}
            for sid in reversed(slides):
                select_page(page,sid)
                reopened[sid]=page.evaluate('()=>window.visualPPTStudioQA.snapshot().elements')==snapshots[sid]
            page.locator(f'[data-pid="{project["id"]}"]').click()
            page.wait_for_function('(id)=>{const s=window.visualPPTStudioQA.snapshot();return s.project?.id===id&&!s.projectLoading}',arg=project['id'])
            report['workflow']['save_switch_refresh_reopen']={'pass':all(reopened.values()),'pages':reopened}
            page.locator('[data-tab="qa"]').click();disable_auto_calibration(page)
            with page.expect_response(lambda r:'/export?' in r.url and r.request.method=='POST',timeout=60000) as event:
                page.locator('#exportEditable').click()
            response=event.value
            if response.status!=200:raise RuntimeError('PPTX export failed: '+response.text())
            export=response.json();current=page.request.get(url+f'/api/projects/{project["id"]}').json()
            report['project_revision']=current['revision'];report['artifact_sha256']=export['artifact_sha256']
            download=page.request.get(url+f'/build/{project["id"]}/'+export['filename'])
            if download.status!=200:raise RuntimeError('Version-bound PPTX download failed')
            (folder/'export.pptx').write_bytes(download.body())
            report['pptx_check']={'native_text':check_native_text(current,folder/'export.pptx'),
                                  'exact_saved_layout':check_saved_layout(current,folder/'export.pptx')}
            (folder/'final_project.json').write_text(json.dumps(current,ensure_ascii=False,indent=2))
            if not args.skip_workflow:
                print('Testing two-page upload/rebuild/save/refresh/export',flush=True)
                report['workflow']['upload_auto_rebuild']=workflow_fixture(page,url,data_root,project)
                report['workflow']['manual_drawer']=workflow_manual_drawer(page,url,report['workflow']['upload_auto_rebuild']['project_id'],folder)
            browser.close()
        report['layout_contract_pass']=all(p['layout_contract_pass'] for p in report['pages'])
        report['workflow_pass']=all(v['pass'] for v in report['workflow'].values())
        report['pptx_pass']=report['pptx_check']['native_text']['ok'] and report['pptx_check']['exact_saved_layout']['ok']
        report['visual_status']='requires_review' if any(p['visual_status']!='pass' for p in report['pages']) else 'pass'
    except Exception as error:
        report['fatal_error']=str(error)
        print('QA error:',error,flush=True)
    finally:
        server.terminate()
        try:server.wait(timeout=10)
        except subprocess.TimeoutExpired:server.kill();server.wait()
        log.close()
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        if not args.preview_only:
            write_html(report,folder)
            print('Report:',folder/'report.html',flush=True)
    if report.get('fatal_error') or not report.get('workflow_pass') or not report.get('pptx_pass') or not report.get('layout_contract_pass') or not all(x.get('pass') for x in report.get('calibration_observations',{}).values()):
        return 1
    if args.strict_visual and report.get('visual_status')!='pass':return 1
    # Review-only OCR/style issues are explicitly reported, not silently accepted.
    return 0


if __name__=='__main__':
    raise SystemExit(main())
