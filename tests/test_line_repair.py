from copy import deepcopy
import pytest
from studio.text_layout import layout_elements,diagnose_lines,repair_line


def line():
    elements=[{'id':'a','kind':'text','text':'中文 ','block_id':'block','line_id':'line','run_id':'a','run_order':0,'ocr_box':[.1,.2,.6,.06],'box':[.1,.2,.6,.06],'style':{'size':20,'font':'Noto Sans','eastAsianFont':'Noto Sans SC'}},
              {'id':'b','kind':'text','text':'English','block_id':'block','line_id':'line','run_id':'b','run_order':1,'ocr_box':[.1,.2,.6,.06],'box':[.1,.2,.6,.06],'style':{'size':20,'font':'Noto Sans'}}]
    return layout_elements(elements)[0]


def test_large_safe_frames_do_not_mean_glyph_overflow():
    elements=line();result=diagnose_lines(elements)
    assert not result[0]['issues']
    assert result[0]['source_extent']['frame_width_pt']>result[0]['source_extent']['ink_width_pt']
    assert len(result[0]['run_ids'])==2


def test_joint_baseline_and_spacing_repair_preserves_content_ids():
    elements=line();elements[1]['box'][0]+=.02;elements[1]['box'][1]+=.02;before=deepcopy(elements)
    assert {'baseline_mismatch','text_spacing'}<=set(diagnose_lines(elements)[0]['issues'])
    proposed,_=repair_line(elements)
    assert elements==before and not diagnose_lines(proposed)[0]['issues']
    assert [e['id'] for e in proposed]==['a','b']
    assert ''.join(e['text'] for e in proposed)=='中文 English'


def test_locked_baseline_is_constraint_for_all_runs():
    elements=line();elements[0]['box'][1]+=.02;elements[0]['layout_locks']={'y':True,'size':True,'font':True}
    proposed,_=repair_line(elements)
    assert proposed[0]['box'][1]==elements[0]['box'][1]
    assert proposed[0]['style']['size']==20 and proposed[0]['layout_locks']==elements[0]['layout_locks']
    assert not diagnose_lines(proposed)[0]['issues']


def test_real_ink_overflow_survives_source_reference_reclassification():
    elements=line();elements[1]['box'][2]=.001
    assert 'ink_outside_frame' in diagnose_lines(elements)[0]['issues']
    elements[1]['box'][0]=.99
    assert 'ink_outside_slide' in diagnose_lines(elements)[0]['issues']


def test_size_consistency_candidate_does_not_change_locked_size():
    elements=line();elements[0]['style']['size']=12;elements[1]['layout_locks']={'size':True}
    proposed,_=repair_line(elements,normalize_size=True)
    assert proposed[1]['style']['size']==20
    assert proposed[0]['style']['size']==20
    assert not diagnose_lines(proposed)[0]['issues'] # unlocked run follows the explicit locked size
