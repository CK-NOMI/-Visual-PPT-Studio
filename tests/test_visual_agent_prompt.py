import asyncio
from pathlib import Path

from PIL import Image

from studio import ai


def test_visual_agent_reflection_includes_issue_region_crops(tmp_path, monkeypatch):
    design = tmp_path / 'design.png'
    before = tmp_path / 'before.png'
    after = tmp_path / 'after.png'
    for path, color in ((design, 'white'), (before, 'gray'), (after, 'white')):
        Image.new('RGB', (120, 80), color).save(path)

    captured = {}

    async def fake_call(messages, *args, **kwargs):
        captured['messages'] = messages
        return {'decision': 'accept', 'issues': [], 'tool_calls': [], 'reason': 'improved'}

    monkeypatch.setattr(ai, 'call_deepseek', fake_call)
    result = asyncio.run(ai.visual_agent_turn(
        {'design': {'width': 13.333, 'height': 7.5}},
        {'id': 'S002', 'elements': []}, design, after,
        phase='reflect', before_render=before,
        tool_feedback={'issues': [{
            'issue_id': 'S002-P1-08', 'description': 'Token与需要之间缺少空格',
            'expected_result': 'Token 需要', 'region': [.1, .2, .4, .1],
        }]},
    ))

    user_content = captured['messages'][1]['content']
    image_blocks = [part for part in user_content if part.get('type') == 'image_url']
    assert len(image_blocks) == 6  # Full Design/before/after plus the same three focused crops.
    assert result['focus_issue_crops'] == 1
    assert any('S002-P1-08' in part.get('text', '') for part in user_content)


def test_visual_agent_prompt_exposes_cross_checked_missing_text_evidence(tmp_path, monkeypatch):
    design=tmp_path/'design.png';render=tmp_path/'render.png'
    Image.new('RGB',(120,80),'white').save(design);Image.new('RGB',(120,80),'white').save(render)
    captured={}
    async def fake_call(messages,*args,**kwargs):
        captured['messages']=messages
        return {'diagnosis':{'issues':[]},'tool_calls':[],'reason':'review'}
    monkeypatch.setattr(ai,'call_deepseek',fake_call)
    proof={'issue_id':'DQA-S002-MISSING-abc123','priority':'P0','source_text':'1',
        'source_box':[.05,.19,.01,.025],'source_ocr_confidence':.999,
        'render_observation':{'actual':'未检测到'},'source_conflicts':[{'text':'T'}]}
    asyncio.run(ai.visual_agent_turn({'design':{'width':13.333,'height':7.5}},
        {'id':'S002','elements':[]},design,render,phase='diagnose',verified_evidence=[proof]))
    prompt=captured['messages'][1]['content'][0]['text']
    assert proof['issue_id'] in prompt
    assert '当前实际渲染QA共同指出的缺字' in prompt
    assert '缺字问题的element_ids应为空数组' in prompt


def test_visual_agent_diagnosis_includes_live_object_values_and_avoids_redundant_read(tmp_path, monkeypatch):
    design=tmp_path/'design.png';render=tmp_path/'render.png'
    Image.new('RGB',(120,80),'white').save(design);Image.new('RGB',(120,80),'white').save(render)
    captured={}
    async def fake_call(messages,*args,**kwargs):
        captured['messages']=messages
        return {'diagnosis':{'issues':[]},'tool_calls':[],'reason':'review'}
    monkeypatch.setattr(ai,'call_deepseek',fake_call)
    current={'id':'target','kind':'text','text':'大模型','box':[.42,.73,.14,.04],
        'style':{'size':16.1,'font':'Arial'},'text_layout':{'font':{'actual_family':'Arial'}},
        'line_id':'line-5','run_order':1}
    asyncio.run(ai.visual_agent_turn({'design':{'width':13.333,'height':7.5}},
        {'id':'S002','elements':[current]},design,render,phase='diagnose'))
    prompt=captured['messages'][1]['content'][0]['text']
    assert '不要先调用get_element或get_page_state' in prompt
    assert 'box":[0.42,0.73,0.14,0.04]' in prompt
    assert 'size":16.1' in prompt
    assert 'actual_family' in prompt
