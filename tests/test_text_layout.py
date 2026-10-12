from copy import deepcopy
import pytest
from studio.text_layout import split_runs, split_regions, layout_elements, layout_project, WIDTH_PT


@pytest.mark.parametrize('text', [
    'Speculative Decoding（推测解码）',
    '使用小模型提前生成候选 Token。',
    '在 T5-XXL（11B）上实现约 2-3 倍推理加速。',
    '中文 English 中文', '  中文  English\t中文  ', '中文 $x^2$ 中文',
])
def test_lossless_split_and_continuous_baseline(text):
    pieces=split_runs(text)
    assert ''.join(p['text'] for p in pieces)==text
    assert all(text[p['char_start']:p['char_end']]==p['text'] for p in pieces)
    original={'id':'a','kind':'text','text':text,'box':[.1,.1,.8,.08],
              'style':{'size':20,'font':'Noto Sans','eastAsianFont':'Noto Sans SC'}}
    elements,report=layout_elements([original])
    assert ''.join(e['text'] for e in elements)==text
    assert all('\n' not in e['text'] for e in elements)
    assert all(e['style']['size']==20 for e in elements)
    assert max(e['text_layout']['baseline_pt'] for e in elements)-min(e['text_layout']['baseline_pt'] for e in elements)<1e-5
    for left,right in zip(elements,elements[1:]):
        assert right['box'][0]*WIDTH_PT==pytest.approx(left['box'][0]*WIDTH_PT+left['text_layout']['advance_pt'],abs=1e-5)
    assert all(e['text_layout']['font']['path'] for e in elements)
    second,_=layout_elements(elements)
    assert second==elements
    assert original['box']==[.1,.1,.8,.08]


def test_multiline_and_repeated_labels_have_stable_distinct_ids():
    rows=split_regions([{'id':'a','text':'中文 English\n第二行','box':[.1,.1,.8,.2]},
                        {'id':'b','text':'Token1','box':[.1,.5,.2,.1]},
                        {'id':'c','text':'Token1','box':[.6,.5,.2,.1]}])
    assert len({r['run_id'] for r in rows})==len(rows)
    assert len({r['line_id'] for r in rows})==4
    assert rows==split_regions([{'id':'a','text':'中文 English\n第二行','box':[.1,.1,.8,.2]},
                               {'id':'b','text':'Token1','box':[.1,.5,.2,.1]},
                               {'id':'c','text':'Token1','box':[.6,.5,.2,.1]}])


def test_legacy_restores_only_evidenced_whitespace_and_keeps_repeated_labels_separate():
    raw='中文 English 中文'
    rows=[{'id':f'r{i}','kind':'text','text':t,'source_text':raw,
           'source_box':[x,.1,w,.04],'box':[x,.1,w,.04],
           'style':{'size':18,'font':'Noto Sans SC'}}
          for i,(t,x,w) in enumerate([('中文',.1,.04),('English',.14,.08),('中文',.22,.04)])]
    rows += [{'id':'label1','kind':'text','text':'Token1','box':[.1,.5,.1,.04],'style':{'size':18}},
             {'id':'label2','kind':'text','text':'Token1','box':[.7,.5,.1,.04],'style':{'size':18}}]
    candidate,report=layout_elements(rows)
    assert ''.join(e['text'] for e in candidate[:3])==raw
    assert len({e['line_id'] for e in candidate[-2:]})==2
    assert len({e['id'] for e in candidate})==len(candidate)


def test_overflow_keeps_size_and_reports_conflict():
    e={'id':'x','kind':'text','text':'Very long English heading'*5,'source_text':'Very long English heading'*5,
       'box':[.9,.1,.08,.03],'style':{'size':30,'font':'Noto Sans'}}
    candidate,report=layout_elements([e])
    assert candidate[0]['style']['size']==30
    assert 'layout_conflict:line_exceeds_slide' in candidate[0]['text_layout']['layout_warning']


def test_unknown_font_fallback_is_explicit_and_uses_exported_family():
    e={'id':'x','kind':'text','text':'English','box':[.1,.1,.4,.1],
       'style':{'size':18,'font':'Missing Font 123456'}}
    candidate,_=layout_elements([e])
    m=candidate[0]['text_layout']
    assert m['font']['fallback'] is True
    assert m['font']['requested_family']=='Missing Font 123456'
    assert candidate[0]['style']['font']==m['font']['actual_family']=='Noto Sans'


def test_manual_position_locks_survive_and_conflicts_are_reported():
    e={'id':'x','kind':'text','text':'中文 English','box':[0,.1,.04,.01],
       'style':{'size':24,'font':'Noto Sans SC'},'layout_locks':{'x':True,'y':True,'w':True,'h':True}}
    candidate,_=layout_elements([e])
    assert all(row['box']==e['box'] for row in candidate)
    assert all(row['style']['size']==24 for row in candidate)
    assert all(row['text_layout']['clipping'] for row in candidate)


def test_formula_has_separate_structural_measurement():
    elements,_=layout_elements([{'id':'f','kind':'text','text':'中文 $\\frac{a}{b}$ 中文',
                                 'box':[.1,.2,.7,.1],'style':{'size':20}}])
    formula=next(e for e in elements if e['kind']=='formula')
    assert 'MathML' in formula['text_layout']['shaping']
    assert formula['text']=='$\\frac{a}{b}$ '
    assert formula['text_layout']['uncertain']


def test_multiline_planned_copy_can_be_saved_then_updated_again(tmp_path,monkeypatch):
    from studio import core
    monkeypatch.setattr(core,'DATA',tmp_path)
    p=core.create_project('Plan','Topic',1)
    s=core.update_slide(p,'S001',{'body':'第一行 English\n第二行 Token'})
    body=[e for e in s['elements'] if e.get('logical_id')=='body']
    assert len({e['line_id'] for e in body})==2
    assert all('\n' not in e['text'] for e in body)
    before=[e['text'] for e in body]
    s=core.update_slide(p,'S001',{'elements':deepcopy(s['elements']),'title':s['title'],'body':s['body']})
    assert [e['text'] for e in s['elements'] if e.get('logical_id')=='body']==before
    s=core.update_slide(p,'S001',{'body':'新正文 English\n新第二行 Token'})
    body=[e for e in s['elements'] if e.get('logical_id')=='body']
    assert len({e['line_id'] for e in body})==2
    assert ''.join(e['text'] for e in body)=='新正文 English新第二行 Token'
