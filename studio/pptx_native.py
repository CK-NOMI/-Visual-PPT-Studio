"""Independent deterministic PPTX builder (python-pptx, no npm/API required).

The design reference is NEVER silently inserted in a supposed fully-editable deck.
Only an approved or explicitly supplied background is used, while editable text
and shapes remain proper OOXML objects. Complex decorative art remains raster.
"""
from __future__ import annotations
import re
from pathlib import Path
import math
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN, MSO_AUTO_SIZE
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt
from pptx.oxml.xmlchemy import OxmlElement
from pptx.oxml.ns import qn
from .core import FILENAME_RE, find_slide, project_dir
from .fonts import default_cjk_font

W, H = 13.333333, 7.5


def checked_style_number(style,key,default,minimum,maximum):
    value=float(style.get(key,default))
    if not math.isfinite(value) or not minimum<=value<=maximum:
        raise ValueError(f'{key} 超出允许范围 {minimum}–{maximum}；导出器不会静默更改已保存参数')
    return value


def east_asian_font() -> str:
    """Return a CJK family available to the local Office/LibreOffice renderer."""
    return default_cjk_font()


def _set_run_fonts(run, latin: str, east_asian: str) -> None:
    # python-pptx's font.name only writes a:latin. Office and LibreOffice use
    # a:ea for CJK glyphs, otherwise Chinese silently falls back to a missing font.
    run.font.name = latin
    rpr = run._r.get_or_add_rPr()
    for tag, family in (('a:latin', latin), ('a:ea', east_asian), ('a:cs', east_asian)):
        node = rpr.find(qn(tag))
        if node is None:
            node = OxmlElement(tag)
            rpr.append(node)
        node.set('typeface', family)


def color(x, fallback='000000'):
    v=str(x or fallback).lstrip('#')
    if not re.fullmatch('[0-9a-fA-F]{6}',v):v=fallback
    return RGBColor.from_string(v.upper())

def coordinates(box):
    if len(box)!=4 or any(not isinstance(x,(int,float)) for x in box):
        raise ValueError('非法元素坐标')
    x,y,w,h=map(float,box)
    if min(x,y)<0 or min(w,h)<=0 or x+w>1.0001 or y+h>1.0001:
        raise ValueError('元素超出页面或尺寸不正确')
    return Inches(W*x),Inches(H*y),Inches(W*w),Inches(H*h)

def asset_path(project, sid, filename):
    if not FILENAME_RE.fullmatch(filename or '') or not filename.upper().startswith(sid+'_'):
        raise ValueError('跨页或非法媒体引用：'+str(filename))
    path=project_dir(project['id'])/'images'/filename
    if not path.is_file():raise FileNotFoundError('图片不存在：'+filename)
    return path

def add_image(slide, path, box, fit='stretch'):
    x,y,w,h=coordinates(box)
    if fit not in ('contain','cover','stretch'):fit='stretch'
    if fit=='stretch':return slide.shapes.add_picture(str(path),x,y,width=w,height=h)
    with Image.open(path) as im: iw,ih=im.size
    if fit=='contain':
        scale=min(w/iw,h/ih);dw=int(iw*scale);dh=int(ih*scale)
        return slide.shapes.add_picture(str(path),x+(w-dw)//2,y+(h-dh)//2,width=dw,height=dh)
    scale=max(w/iw,h/ih);dw=int(iw*scale);dh=int(ih*scale)
    shape=slide.shapes.add_picture(str(path),x,y,width=w,height=h)
    if dw>w: shape.crop_left=shape.crop_right=(1-w/dw)/2
    if dh>h: shape.crop_top=shape.crop_bottom=(1-h/dh)/2
    return shape

def build(project:dict, output:Path, mode='editable'):
    if mode not in ('editable','raster'):raise ValueError('非法导出模式')
    ppt=Presentation();ppt.slide_width=Inches(W);ppt.slide_height=Inches(H)
    for page in project['slides']:
        sid=page['id'];s=ppt.slides.add_slide(ppt.slide_layouts[6])
        bg=page.get('background') or project['design']['bg']
        s.background.fill.solid();s.background.fill.fore_color.rgb=color(bg,'FFFFFF')
        imgs=page.get('images',{})
        if mode=='raster':
            if imgs.get('design'): add_image(s,asset_path(project,sid,imgs['design']),[0,0,1,1])
            continue
        if imgs.get('clean'):
            meta=page.get('image_metadata',{}).get('clean',{})
            if meta.get('stale_source'):raise ValueError(f'{sid} 无字背景与新版设计稿不对应，请重新审核')
            add_image(s,asset_path(project,sid,imgs['clean']),[0,0,1,1])
        for obj in page.get('elements',[]):
            kind=obj.get('kind'); st=obj.get('style') or {}
            x,y,w,h=coordinates(obj['box'])
            if kind=='text':
                size=checked_style_number(st,'size',18,4,160)
                render_text=str(obj.get('text',''))
                if any(c in render_text for c in '\r\n\v\f\u0085\u2028\u2029'):
                    raise ValueError(f'{sid}/{obj["id"]} 仍是多行文字，请先生成统一布局候选并保存')
                east_font=st.get('eastAsianFont') or st.get('font') or project['design'].get('font') or east_asian_font()
                tracking=checked_style_number(st,'char_spacing_pt',0,-5,20)
                margin_left=checked_style_number(st,'margin_left_pt',0,0,72)
                margin_right=checked_style_number(st,'margin_right_pt',0,0,72)
                line_texts=[render_text]
                for line in line_texts:
                    # Preserve even empty editable placeholders from the scene.
                    # The export graph must not silently discard saved objects.
                    line_y=y
                    line_height=h
                    tb=s.shapes.add_textbox(x,line_y,w,line_height)
                    base_name=str(obj.get('id','text'))
                    tb.name=base_name
                    tf=tb.text_frame;tf.clear();tf.word_wrap=False
                    tf.auto_size=MSO_AUTO_SIZE.NONE
                    tf.margin_left=Pt(margin_left)
                    tf.margin_right=Pt(margin_right)
                    tf.margin_top=Pt(checked_style_number(st,'margin_top_pt',0,0,72))
                    tf.margin_bottom=Pt(checked_style_number(st,'margin_bottom_pt',0,0,72))
                    tf.vertical_anchor={'top':MSO_ANCHOR.TOP,'middle':MSO_ANCHOR.MIDDLE,'bottom':MSO_ANCHOR.BOTTOM}.get(st.get('vertical_align'),MSO_ANCHOR.TOP)
                    p=tf.paragraphs[0];p.text=line
                    p.line_spacing=checked_style_number(st,'line_spacing',1.05,.7,2.5)
                    p.space_before=Pt(checked_style_number(st,'paragraph_space_before_pt',0,0,72))
                    p.space_after=Pt(checked_style_number(st,'paragraph_space_after_pt',0,0,72))
                    p.alignment={'left':PP_ALIGN.LEFT,'center':PP_ALIGN.CENTER,'right':PP_ALIGN.RIGHT}.get(st.get('align'),PP_ALIGN.LEFT)
                    for r in p.runs:
                        latin_font=st.get('font') or project['design'].get('font') or 'Arial'
                        _set_run_fonts(r,latin_font,east_font)
                        r.font.size=Pt(size)
                        r.font.bold=bool(st.get('bold',False))
                        r.font.italic=bool(st.get('italic',False))
                        r.font.color.rgb=color(st.get('color'),project['design']['ink'])
                        # DrawingML run spacing is in 1/100 pt; python-pptx
                        # currently has no public Font.spacing property.
                        if abs(tracking)>1e-6:
                            r._r.get_or_add_rPr().set('spc',str(round(tracking*100)))
            elif kind=='formula':
                from .equations import append_omml
                tb=s.shapes.add_textbox(x,y,w,h);tf=tb.text_frame
                tb.name=str(obj['id'])
                tf.clear();tf.word_wrap=False;tf.auto_size=MSO_AUTO_SIZE.NONE
                tf.margin_left=tf.margin_right=tf.margin_top=tf.margin_bottom=0
                tf.vertical_anchor=MSO_ANCHOR.TOP if obj.get('text_layout') else MSO_ANCHOR.MIDDLE
                paragraph=tf.paragraphs[0]
                append_omml(paragraph,str(obj.get('text','')),float(st.get('size',22)),st.get('align','center'))
            elif kind in ('rect','ellipse'):
                preset=MSO_SHAPE.OVAL if kind=='ellipse' else MSO_SHAPE.RECTANGLE
                sh=s.shapes.add_shape(preset,x,y,w,h);sh.fill.solid()
                sh.fill.fore_color.rgb=color(st.get('fill'),project['design']['accent'])
                sh.line.color.rgb=color(st.get('stroke'),st.get('fill') or project['design']['accent'])
                sh.line.width=Pt(max(0,float(st.get('strokeWidth',0))))
            elif kind=='line':
                from pptx.enum.shapes import MSO_CONNECTOR
                sh=s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,x,y,x+w,y+h)
                sh.line.color.rgb=color(st.get('stroke'),project['design']['accent'])
                sh.line.width=Pt(max(.25,float(st.get('strokeWidth',2))))
                if st.get('arrow'):
                    from pptx.oxml.xmlchemy import OxmlElement
                    line_props=sh._element.spPr.get_or_add_ln()
                    end=OxmlElement('a:tailEnd');end.set('type','triangle');line_props.append(end)
            elif kind=='image':
                add_image(s,asset_path(project,sid,obj['asset']),obj['box'],st.get('fit','stretch'))
            else:raise ValueError('不支持的对象类型：'+str(kind))
    ppt.core_properties.comments='Visual PPT Studio; stage renderer: Chromium browser; compatibility target: WPS Office for Mac; layout version 2'
    output.parent.mkdir(parents=True,exist_ok=True);ppt.save(str(output))
    from .render_acceptance import embed_manifest
    embed_manifest(output,project,mode)
    return {'slides':len(ppt.slides),'backend':'python-pptx','native_elements':sum(len(s.shapes) for s in ppt.slides)}
