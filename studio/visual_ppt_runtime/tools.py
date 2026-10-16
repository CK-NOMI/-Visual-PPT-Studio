"""Small, explicit model tool surface. Host-only decisions are never exposed."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Any
from pathlib import Path
from .errors import EditingError,RendererUnavailable,ValidationError
from .transactions import EditingSession
from .scene import signature

TOOLS = [
  {'type':'function','function':{'name':'get_page_state','description':'Read paginated compact slide objects with stable IDs, text, precise box, style and groups. Use get_element for full detail.',
    'parameters':{'type':'object','properties':{'slide_id':{'type':'string'},'offset':{'type':'integer','minimum':0,'default':0},'limit':{'type':'integer','minimum':1,'maximum':60,'default':25}},'required':['slide_id']}}},
  {'type':'function','function':{'name':'get_element','description':'Inspect exact element properties and current text.',
    'parameters':{'type':'object','properties':{'slide_id':{'type':'string'},'element_id':{'type':'string'}},'required':['slide_id','element_id']}}},
  {'type':'function','function':{'name':'batch_edit','description':('Apply ONE atomic issue transaction to an isolated draft. Supported action forms: '
      'set_properties {element_id,changes:{box OR box_pt:[x,y,w,h] 960x540pt,rotation,style:{font,eastAsianFont,size,bold,italic,underline,strike,color,char_spacing_pt,line_spacing,align,vertical_align,margin_left_pt,margin_right_pt,margin_top_pt,margin_bottom_pt,paragraph_space_before_pt,paragraph_space_after_pt},text,rich_runs:[{text,style}]}}; '
      'set_rich_runs {element_id,runs:[{text,style}]}; set_geometry {element_id,box OR box_pt}; nudge_element {element_id,delta_pt:[dx,dy]}; '
      'align_elements {element_ids,alignment:left|right|hcenter|top|bottom|vcenter}; distribute_elements {element_ids,axis:horizontal|vertical}; '
      'create_element {element:{id,kind,text,style,box,source_region_id},evidence_id}; delete_element {element_id}; '
      'duplicate_element {element_id,new_id,box}; reorder_element {element_id,index}; set_background {color}. '
      'Formula kind text is LaTeX, formula style supports size,align. Text supports mixed Chinese/English font and segment-level formatting. '
      'Do not assume unsupported opacity or arbitrary numeric font weight. Coordinates and values are validated. Draft only, host must approve.'),
    'parameters':{'type':'object','properties':{'slide_id':{'type':'string'},'issue_id':{'type':'string'},'tx_id':{'type':'string'},
      'operations':{'type':'array','items':{'type':'object'}}},'required':['slide_id','issue_id','operations']}}},
  {'type':'function','function':{'name':'undo_edit','description':'Undo the last active transaction in the draft.',
    'parameters':{'type':'object','properties':{},'additionalProperties':False}}},
  {'type':'function','function':{'name':'redo_edit','description':'Redo the most recently undone draft transaction.',
    'parameters':{'type':'object','properties':{},'additionalProperties':False}}},
  {'type':'function','function':{'name':'get_region_screenshot','description':'Crop a rectangle from the most recent REAL render. box normalized [x,y,w,h].',
    'parameters':{'type':'object','properties':{'box':{'type':'array','items':{'type':'number'},'minItems':4,'maxItems':4}},'required':['box']}}},
  {'type':'function','function':{'name':'get_draft_status','description':'Report exact draft and transaction states.',
    'parameters':{'type':'object','properties':{},'additionalProperties':False}}},
  {'type':'function','function':{'name':'render_page','description':'Render current DRAFT with actual configured renderer and return screenshot metadata.',
    'parameters':{'type':'object','properties':{'slide_id':{'type':'string'}},'required':['slide_id']}}},
]

@dataclass
class RenderOutcome:
    image_path:str
    renderer:str
    project_signature:str
    image_sha256:str
    pptx_path:str|None=None
    pptx_sha256:str|None=None
    revision:int|None=None

class EditingTools:
    def __init__(self,session:EditingSession,renderer:Callable[[dict,str],RenderOutcome]|None=None):
        self.session=session
        self.renderer=renderer
        self.last_render:RenderOutcome|None=None
        self.render_count=0

    def dispatch(self,name:str,args:dict)->dict:
        if not isinstance(args,dict):return {'ok':False,'error':'arguments must be object'}
        try:
            if name=='get_page_state':
                r=self.session.page(args['slide_id'])
                elems=r['slide'].get('elements',[])
                offset=args.get('offset',0);limit=args.get('limit',25)
                # Vision models sometimes emit 0, strings or page-size values
                # outside the advertised schema. Keep the read-only tool useful
                # while bounding the actual response size.
                if type(offset) is not int or offset<0:offset=0
                if type(limit) is not int or limit<=0:limit=25
                limit=min(60,limit)
                compact=[{'id':e['id'],'kind':e['kind'],'text':e.get('text',''),
                          'box':e['box'],'style':e.get('style',{}),
                          'line_id':e.get('line_id'),
                          'semantic_group_id':e.get('semantic_group_id'),
                          'source_region_id':e.get('source_region_id')} for e in elems[offset:offset+limit]]
                return {'ok':True,'result':{'slide_id':args['slide_id'],'elements':compact,'total':len(elems),
                       'next_offset':offset+len(compact) if offset+len(compact)<len(elems) else None,
                       'groups':r['groups'],'draft_version':r['draft_version'],'draft_sha':r['draft_sha']}}
            if name=='get_element':
                return {'ok':True,'result':self.session.get_element(args['slide_id'],args['element_id'])}
            if name=='batch_edit':
                tx=self.session.execute(args['slide_id'],args['issue_id'],args['operations'],tx_id=args.get('tx_id'))
                return {'ok':True,'result':tx,'note':'applied to draft only; not accepted by host QA or source project'}
            if name=='undo_edit':
                return {'ok':True,'result':self.session.undo()}
            if name=='redo_edit':
                return {'ok':True,'result':self.session.redo()}
            if name=='get_region_screenshot':
                if not self.last_render:raise RendererUnavailable('render_page first')
                if self.last_render.project_signature!=signature(self.session.working):
                    raise RendererUnavailable('draft changed since screenshot; call render_page before cropping')
                from PIL import Image
                from .scene import validate_box
                box=validate_box(args.get('box'))
                with Image.open(self.last_render.image_path) as img:
                    x,y,w,h=box
                    region=img.crop((int(x*img.width),int(y*img.height),int((x+w)*img.width),int((y+h)*img.height)))
                    if min(region.size)<2:raise ValidationError('crop region too small')
                    path=Path(self.last_render.image_path).with_name('crop-'+signature(box)[:12]+'.png')
                    region.save(path)
                return {'ok':True,'result':{'image_path':str(path),'source_image_sha256':self.last_render.image_sha256,
                   'source_project_signature':self.last_render.project_signature}}
            if name=='get_draft_status':
                return {'ok':True,'result':{'version':self.session._version,'signature':signature(self.session.working),
                    'transactions':[{'tx_id':t.tx_id,'issue_id':t.issue_id,'status':t.status} for t in self.session.transactions]}}
            if name=='render_page':
                if not self.renderer:raise RendererUnavailable('no renderer callback configured')
                sid=args['slide_id']
                if sid not in self.session.policy.allowed_slides:raise ValidationError('slide not authorized')
                observed=self.renderer(self.session.working,sid)
                if not isinstance(observed,RenderOutcome) or observed.project_signature!=signature(self.session.working):
                    raise RendererUnavailable('renderer did not bind results to exact draft scene')
                if not Path(observed.image_path).is_file():raise RendererUnavailable('rendered screenshot missing')
                self.last_render=observed;self.render_count+=1
                return {'ok':True,'result':observed.__dict__}
            raise ValidationError('unknown tool name')
        except (EditingError,ValueError,TypeError,KeyError) as exc:
            return {'ok':False,'error':str(exc),'tool':name}
