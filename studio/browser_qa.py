"""Object diagnostics from real DOM/canvas glyph metrics and rendered pixels."""
from __future__ import annotations
from collections import Counter, defaultdict
import html
import json
from pathlib import Path
from PIL import Image
from .quality import _contrast
from .core import project_dir


def assess_page(project, slide, observation, screenshot, folder, visual_comparison=None):
    stage=observation['stage'];scale=stage['width']/960
    expected={e['id']:e for e in slide['elements'] if e['kind'] in ('text','formula') and str(e.get('text','')).strip()}
    observed={o['id']:o for o in observation['objects']}
    image=Image.open(screenshot).convert('RGB')
    target=Image.open(project_dir(project['id'])/'images'/slide['images']['design']).convert('RGB')
    aligned=image.resize(target.size,Image.Resampling.LANCZOS)
    aligned.save(folder/f'{slide["id"]}_aligned.png')
    from . import reconstruction as rc
    import numpy as np
    objects=[];lines=defaultdict(list)
    clean=None
    if slide['images'].get('clean'):
        clean=Image.open(project_dir(project['id'])/'images'/slide['images']['clean']).convert('RGB').resize(target.size)
    clean_ocr=[]
    if clean is not None and rc.available_ocr().get('rapidocr'):
        clean_ocr=[r for r in rc._rapidocr(np.asarray(clean)) if float(r.get('confidence',0))>=.85]
    render_diffs=(visual_comparison or {}).get('objective_score',{}).get('differences',[])
    for id_,e in expected.items():
        dom=observed.get(id_);layout=e.get('text_layout') or {};issues=[]
        st=e.get('style') or {};size=float(st.get('size',18));box=e['box']
        if dom is None:issues.append({'type':'missing_dom_object','severity':'error'})
        else:
            if e['kind']=='text' and dom['text']!=e['text']:issues.append({'type':'text_or_whitespace_changed','severity':'error'})
            if e['kind']=='text' and len(dom['range_rects'])>1 and max(r['y'] for r in dom['range_rects'])-min(r['y'] for r in dom['range_rects'])>size*scale*.5:
                issues.append({'type':'unexpected_line_wrap','severity':'error'})
            if abs(dom['font_size_px']-size*scale)>.15:issues.append({'type':'browser_changed_font_size','severity':'error','expected_px':size*scale,'actual_px':dom['font_size_px']})
            if max(abs(a-b) for a,b in zip(dom['box'],box))>.00005:issues.append({'type':'dom_box_differs_from_layout','severity':'error','expected':box,'actual':dom['box']})
            if layout and e['kind']=='text' and abs(dom['baseline_px']-layout['baseline_pt']*scale)>.5:
                issues.append({'type':'baseline_differs_from_layout','severity':'error','delta_px':dom['baseline_px']-layout['baseline_pt']*scale})
            if dom.get('font_status')!='loaded' and layout:issues.append({'type':'font_not_loaded','severity':'error','font_status':dom.get('font_status')})
            if dom.get('font_fallback'):issues.append({'type':'explicit_font_fallback','severity':'review'})
            if dom.get('platform_fonts') and layout and not all(f.get('isCustomFont') for f in dom['platform_fonts']):
                issues.append({'type':'browser_system_fallback','severity':'error','fonts':dom['platform_fonts']})
            ink=dom.get('ink_box')
            if ink:
                x,y,w,h=box;tol=1.0
                if ink['x']<x*stage['width']-tol or ink['y']<y*stage['height']-tol or ink['x']+ink['width']>(x+w)*stage['width']+tol or ink['y']+ink['height']>(y+h)*stage['height']+tol:
                    issues.append({'type':'glyph_outside_frame','severity':'error','ink_box':ink})
                if ink['x']<-.5 or ink['y']<-.5 or ink['x']+ink['width']>stage['width']+.5 or ink['y']+ink['height']>stage['height']+.5:
                    issues.append({'type':'slide_edge_clipping','severity':'error'})
                backend=layout.get('ink_box_pt')
                if backend and abs(ink['height']-(backend[3]-backend[1])*scale)>max(.75,ink['height']*.08):
                    issues.append({'type':'browser_backend_glyph_height','severity':'review','expected_px':(backend[3]-backend[1])*scale,'actual_px':ink['height']})
            for warning in layout.get('layout_warning',[]):
                severity='error' if warning.startswith('layout_conflict:') else 'review'
                issues.append({'type':warning,'severity':severity})
        # Use matched image evidence as an additional measurement, not as a
        # replacement for the exact DOM export contract.
        for diff in render_diffs:
            if diff.get('id')==id_ or id_ in diff.get('objects',[]):
                issues.append({'type':'image_'+diff['type'],'severity':'review','measurement':diff})
        source_box=e.get('ocr_box') or e.get('source_box') or box
        crop_box=_union(source_box,box)
        padding=.015
        x,y,w,h=crop_box
        pixels=(max(0,int((x-padding)*target.width)),max(0,int((y-padding)*target.height)),
                min(target.width,int((x+w+padding)*target.width)),min(target.height,int((y+h+padding)*target.height)))
        # Avoid a row-capacity frame turning every crop into half a slide.
        if dom and dom.get('ink_box'):
            ink=dom['ink_box'];actual=[ink['x']/stage['width'],ink['y']/stage['height'],ink['width']/stage['width'],ink['height']/stage['height']]
            x,y,w,h=_union(source_box,actual)
            pixels=(max(0,int((x-padding)*target.width)),max(0,int((y-padding)*target.height)),
                    min(target.width,int((x+w+padding)*target.width)),min(target.height,int((y+h+padding)*target.height)))
        safe=''.join(c if c.isalnum() else '_' for c in id_)[:90]
        crop_name=f'{slide["id"]}_{safe}_compare.png'
        left=target.crop(pixels);right=aligned.crop(pixels)
        pair=Image.new('RGB',(left.width+right.width,left.height),'white');pair.paste(left);pair.paste(right,(left.width,0));pair.save(folder/crop_name)
        if clean is not None:
            ink=(dom or {}).get('ink_box')
            if ink:
                bg_pixels=(max(0,int(ink['x']/stage['width']*target.width)),max(0,int(ink['y']/stage['height']*target.height)),
                           min(target.width,int((ink['x']+ink['width'])/stage['width']*target.width)+1),
                           min(target.height,int((ink['y']+ink['height'])/stage['height']*target.height)+1))
            else:bg_pixels=pixels
            sample=clean.crop(bg_pixels)
            if sample.width and sample.height:
                median=np.median(np.asarray(sample).reshape(-1,3),axis=0)
                ratio=_contrast(st.get('color','000000'),median)
                if ratio<3:issues.append({'type':'low_background_contrast','severity':'review','ratio':round(ratio,2)})
            from .quality import intersects,visible_glyph_box
            vb=visible_glyph_box(e)
            for residual in clean_ocr:
                rb=residual['box']
                if rc._plain_text(residual['text'])==rc._plain_text(e['text']) and intersects(vb,rb)/max(1e-9,min(vb[2]*vb[3],rb[2]*rb[3]))>.5:
                    issues.append({'type':'suspected_clean_text_residual','severity':'visual_error','text':residual['text'],'box':rb,'confidence':residual['confidence']})
        row={'slide_id':slide['id'],'id':id_,'line_id':e.get('line_id'),'run_id':e.get('run_id'),
             'source_text':e.get('source_text',e.get('text','')),'final_text':e['text'],
             'font_size_pt':size,'font':layout.get('font'),'position':box,
             'dom':dom,'anomalies':issues,'crop':crop_name}
        objects.append(row);lines[e.get('line_id',id_)].append(row)
    for index,a in enumerate(objects):
        ab=(a['dom'] or {}).get('ink_box')
        if not ab:continue
        for b in objects[index+1:]:
            bb=(b['dom'] or {}).get('ink_box')
            if not bb:continue
            area=max(0,min(ab['x']+ab['width'],bb['x']+bb['width'])-max(ab['x'],bb['x']))*max(0,min(ab['y']+ab['height'],bb['y']+bb['height'])-max(ab['y'],bb['y']))
            ratio=area/max(1e-6,min(ab['width']*ab['height'],bb['width']*bb['height']))
            if ratio>.12:
                b['anomalies'].append({'type':'visible_glyph_overlap','severity':'visual_error','other_id':a['id'],'ratio':round(ratio,3),
                                       'instruction':'Review OCR/source grouping; overlapping transparent frames alone are not an error'})
    for rows in lines.values():
        rows.sort(key=lambda r:expected[r['id']].get('run_order',0))
        for left,right in zip(rows,rows[1:]):
            a=(left['dom'] or {}).get('ink_box');b=(right['dom'] or {}).get('ink_box')
            if not a or not b:continue
            le,re=expected[left['id']],expected[right['id']]
            lm,rm=le['text_layout'],re['text_layout']
            actual_gap=b['x']-(a['x']+a['width'])
            expected_gap=(re['box'][0]*960+rm['ink_box_pt'][0]-le['box'][0]*960-lm['ink_box_pt'][2])*scale
            error=abs(actual_gap-expected_gap)
            right['adjacent_run_gap']={'previous_id':left['id'],'expected_px':expected_gap,'actual_px':actual_gap,'delta_px':error,
                                       'raw_whitespace':lm.get('whitespace',[])+rm.get('whitespace',[])}
            if error>max(1.0,float(re['style'].get('size',18))*scale*.06):
                right['anomalies'].append({'type':'run_spacing_differs_from_layout','severity':'error',**right['adjacent_run_gap']})
    seen=Counter(o['id'] for o in observation['objects'])
    extra=[{'id':id_,'type':'unexpected_or_duplicate_dom','count':count} for id_,count in seen.items() if id_ not in expected or count>1]
    errors=sum(any(a['severity']=='error' for a in row['anomalies']) for row in objects)+len(extra)
    reviews=sum(any(a['severity'] in ('review','visual_error') for a in row['anomalies']) for row in objects)
    unmapped=[d for d in render_diffs if not d.get('id') and not any(d.get('objects',[]))]
    comparison_status=(visual_comparison or {}).get('objective_score',{}).get('status')
    return {'slide_id':slide['id'],'objects':objects,'extra_objects':extra,'failed_objects':errors,
            'review_objects':reviews,'layout_contract_pass':errors==0,
            'visual_status':'requires_review' if reviews or errors or unmapped or comparison_status not in ('pass',None) else 'pass',
            'unmapped_image_evidence':unmapped,'clean_ocr_candidates':clean_ocr,
            'visual_error_objects':sum(any(a['severity']=='visual_error' for a in row['anomalies']) for row in objects),
            'image_comparison':visual_comparison,'stage':stage,'browser':observation.get('browser'),
            'meaning':'DOM字体/字号/坐标/空白/基线/行内字形间距为独立检查；OCR/颜色/背景为图片证据，不以总分替代逐对象验收。'}


def _union(a,b):
    x=min(a[0],b[0]);y=min(a[1],b[1]);r=max(a[0]+a[2],b[0]+b[2]);bottom=max(a[1]+a[3],b[1]+b[3])
    return [x,y,r-x,bottom-y]


def write_html(report,folder):
    h=html.escape;rows=[]
    for page in report['pages']:
        rows.append(f'<h2>{h(page["slide_id"])}：布局失败 {page["failed_objects"]}，视觉待复核 {page["review_objects"]}</h2>'
                    f'<p><a href="{page["slide_id"]}_page.png">完整网页截图</a> · <a href="{page["slide_id"]}_stage.png">实际幻灯片截图</a></p>')
        rows.append('<img class="slide" src="'+page['slide_id']+'_stage.png"><table><tr><th>对象／行</th><th>文字／参数</th><th>诊断</th><th>原稿 → 浏览器局部对照</th></tr>')
        for obj in page['objects']:
            failures='; '.join(a['type'] for a in obj['anomalies']) or '布局契约通过'
            cls='fail' if any(a['severity']=='error' for a in obj['anomalies']) else 'review' if obj['anomalies'] else 'pass'
            details=json.dumps({'source_text':obj['source_text'],'position':obj['position'],'dom':obj['dom'],'anomalies':obj['anomalies']},ensure_ascii=False,indent=2)
            rows.append(f'<tr class="{cls}"><td>{h(obj["id"])}<br><small>{h(str(obj["line_id"]))}<br>{h(str(obj["run_id"]))}</small></td>'
                        f'<td><pre>{h(obj["final_text"])}</pre>{obj["font_size_pt"]}pt</td><td>{h(failures)}<details><summary>实际参数</summary><pre>{h(details)}</pre></details></td>'
                        f'<td><a href="{obj["crop"]}"><img class="crop" src="{obj["crop"]}"></a></td></tr>')
        rows.append('</table>')
    body=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Visual PPT Studio 浏览器回归</title>
<style>body{{font-family:system-ui;margin:30px;color:#18304b}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:8px;vertical-align:top}}pre{{white-space:pre-wrap;max-width:500px}}.slide{{width:min(100%,1100px)}}.crop{{max-width:420px;max-height:180px}}.fail{{background:#fff0ec}}.review{{background:#fffbe8}}small{{font-size:10px}}</style>
<h1>真实浏览器视觉回归</h1><p>Chromium · 字体文件明确加载 · 原稿／截图对齐 · PPTX结构独立检查。</p>
<p>布局契约：{report.get('layout_contract_pass')} · 工作流：{report.get('workflow_pass')} · PPTX：{report.get('pptx_pass')} · 视觉：{h(str(report.get('visual_status')))}</p>
<p>项目版本 {report.get('project_revision')} · <a href="report.json">JSON完整诊断</a> · <a href="export.pptx">本次PPTX</a></p>
<details open><summary>工作流与导出结果</summary><pre>{h(json.dumps(report.get('workflow'),ensure_ascii=False,indent=2))}</pre><pre>{h(json.dumps(report.get('pptx_check'),ensure_ascii=False,indent=2))}</pre></details>
{''.join(rows)}</html>'''
    (folder/'report.html').write_text(body,encoding='utf-8')
