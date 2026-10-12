"""Safe conversion from editable LaTeX source to native Office Math (OMML)."""
from __future__ import annotations

from lxml import etree

MATHML_NS = 'http://www.w3.org/1998/Math/MathML'
OMML_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
A_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'
A14_NS = 'http://schemas.microsoft.com/office/drawing/2010/main'

_TAGS = {
    'math', 'mrow', 'mi', 'mn', 'mo', 'mtext', 'ms', 'mspace', 'mfrac',
    'msqrt', 'mroot', 'msub', 'msup', 'msubsup', 'munder', 'mover',
    'munderover', 'mfenced', 'mtable', 'mtr', 'mtd', 'mstyle', 'mpadded',
}


def to_mathml(latex: str) -> str:
    """Convert bounded user LaTeX into checked Presentation MathML for preview."""
    if not isinstance(latex, str) or not latex.strip():
        raise ValueError('公式不能为空，请输入 LaTeX 公式源码')
    if len(latex) > 2000:
        raise ValueError('公式源码最多 2000 个字符')
    try:
        from latex2mathml.converter import convert
        expression = latex.strip()
        for left, right in (('\\(', '\\)'), ('\\[', '\\]'), ('$', '$')):
            if expression.startswith(left) and expression.endswith(right):
                expression = expression[len(left):-len(right)]
                break
        raw = convert(expression, display='block')
        root = etree.fromstring(raw.encode('utf-8'))
    except Exception as exc:
        raise ValueError('公式无法解析，请检查 LaTeX 语法：' + str(exc)[:180]) from exc
    nodes = list(root.iter())
    if len(nodes) > 500:
        raise ValueError('公式结构过于复杂（最多 500 个节点）')
    for node in nodes:
        tag = etree.QName(node).localname
        if tag not in _TAGS or (etree.QName(node).namespace or MATHML_NS) != MATHML_NS:
            raise ValueError('暂不支持公式结构：' + tag)
        children = list(node)
        exact = {'mfrac':2,'mroot':2,'msub':2,'msup':2,'msubsup':3,
                 'munder':2,'mover':2,'munderover':3}
        if tag in exact and len(children) != exact[tag]:
            raise ValueError(f'{tag} 结构不完整，请补全公式')
        if tag == 'mspace' and not node.get('width'):
            raise ValueError('公式空格结构无效')
    if root.find('.//{%s}merror' % MATHML_NS) is not None:
        raise ValueError('公式解析器检测到语法错误')
    return etree.tostring(root, encoding='unicode')


def measure_formula(latex: str, size: float) -> dict:
    """Measure MathML structure rather than the width of literal LaTeX source.

    Fractions, roots and scripts have separate extents. Office's OMML renderer
    remains the final authority; approximation is explicitly reported.
    """
    from .fonts import measure_text
    root = etree.fromstring(to_mathml(latex).encode())
    face = measure_text('x', size, 'Cambria Math')
    def extent(node, scale=1):
        tag = etree.QName(node).localname
        children = list(node)
        if not children:
            m = measure_text(''.join(node.itertext()) or ' ', size*scale, 'Cambria Math')
            return m['advance_pt'], m['ascent_pt'], m['descent_pt']
        parts = [extent(child, scale) for child in children]
        if tag == 'mfrac':
            return max(p[0] for p in parts)+size*scale*.2, sum(p[1]+p[2] for p in parts)/2+size*scale*.25, sum(p[1]+p[2] for p in parts)/2
        if tag in ('msub', 'msup', 'msubsup'):
            scripts = [extent(child, scale*.7) for child in children[1:]]
            return parts[0][0]+max(p[0] for p in scripts), parts[0][1]+(scripts[-1][1]*.6 if tag!='msub' else 0), parts[0][2]+(scripts[0][1]*.6 if tag!='msup' else 0)
        if tag in ('msqrt', 'mroot'):
            return sum(p[0] for p in parts)+size*scale*.6, max(p[1] for p in parts)+size*scale*.15, max(p[2] for p in parts)
        return sum(p[0] for p in parts), max(p[1] for p in parts), max(p[2] for p in parts)
    advance, ascent, descent = extent(root)
    return {'advance_pt': advance, 'ink_box_pt': [0, -ascent, advance, descent],
            'ascent_pt': ascent, 'descent_pt': descent, 'line_height_pt': ascent+descent,
            'font': face['font'], 'missing_characters': '', 'uncertain': True,
            'shaping': 'MathML structural estimate; OMML requires rendered verification'}


def _q(ns: str, tag: str) -> str:
    return f'{{{ns}}}{tag}'


def _math_children(source: etree._Element, target: etree._Element, font_size: float = 22) -> None:
    for child in source:
        _math_node(child, target, font_size)


def _math_node(source: etree._Element, parent: etree._Element, font_size: float = 22) -> None:
    tag = etree.QName(source).localname
    children = list(source)
    if tag in ('math', 'mrow', 'mstyle', 'mpadded'):
        _math_children(source, parent, font_size)
        return
    if tag in ('mi', 'mn', 'mo', 'mtext', 'ms', 'mspace'):
        text = source.get('width') if tag == 'mspace' else ''.join(source.itertext())
        if tag == 'mspace':
            text = '\u2009' if text not in ('0em', '0px', '0pt') else ''
        if not text:
            return
        run = etree.SubElement(parent, _q(OMML_NS, 'r'))
        props = etree.SubElement(run, _q(A_NS, 'rPr'), sz=str(round(font_size * 100)))
        etree.SubElement(props, _q(A_NS, 'latin'), typeface='Cambria Math')
        value = etree.SubElement(run, _q(OMML_NS, 't'))
        if text[:1].isspace() or text[-1:].isspace():
            value.set(_q('http://www.w3.org/XML/1998/namespace', 'space'), 'preserve')
        value.text = text
        return
    if tag == 'mfrac':
        if len(children) != 2: raise ValueError('分数必须包含分子和分母')
        fraction = etree.SubElement(parent, _q(OMML_NS, 'f'))
        num = etree.SubElement(fraction, _q(OMML_NS, 'num'));_math_node(children[0], num, font_size)
        den = etree.SubElement(fraction, _q(OMML_NS, 'den'));_math_node(children[1], den, font_size)
        return
    if tag in ('msqrt', 'mroot'):
        radical = etree.SubElement(parent, _q(OMML_NS, 'rad'))
        props = etree.SubElement(radical, _q(OMML_NS, 'radPr'))
        etree.SubElement(props, _q(OMML_NS, 'degHide'), {_q(OMML_NS, 'val'): '1' if tag == 'msqrt' else '0'})
        degree = etree.SubElement(radical, _q(OMML_NS, 'deg'))
        expression = etree.SubElement(radical, _q(OMML_NS, 'e'))
        if tag == 'msqrt':
            _math_children(source, expression, font_size)
        else:
            if len(children) != 2: raise ValueError('根式格式无效')
            _math_node(children[1], degree, font_size * .7);_math_node(children[0], expression, font_size)
        return
    script_map = {'msub': ('sSub', ('e','sub')), 'msup': ('sSup', ('e','sup')),
                  'msubsup': ('sSubSup', ('e','sub','sup'))}
    if tag in script_map:
        name, order = script_map[tag]
        if len(children) != len(order): raise ValueError('上下标格式无效')
        obj = etree.SubElement(parent, _q(OMML_NS, name))
        for source_child, name_child in zip(children, order):
            dest = etree.SubElement(obj, _q(OMML_NS, name_child));_math_node(source_child, dest, font_size * (.7 if name_child != 'e' else 1))
        return
    if tag in ('munder', 'mover', 'munderover'):
        if len(children) != (3 if tag == 'munderover' else 2): raise ValueError('上下限格式无效')
        if tag == 'munderover':
            obj = etree.SubElement(parent, _q(OMML_NS, 'limUpp'))
            base = etree.SubElement(obj, _q(OMML_NS, 'e'))
            lower = etree.SubElement(base, _q(OMML_NS, 'limLow'))
            lower_base = etree.SubElement(lower, _q(OMML_NS, 'e'));_math_node(children[0], lower_base, font_size)
            lower_lim = etree.SubElement(lower, _q(OMML_NS, 'lim'));_math_node(children[1], lower_lim, font_size * .7)
            limit = etree.SubElement(obj, _q(OMML_NS, 'lim'));_math_node(children[2], limit, font_size * .7)
        else:
            obj = etree.SubElement(parent, _q(OMML_NS, 'limLow' if tag == 'munder' else 'limUpp'))
            base = etree.SubElement(obj, _q(OMML_NS, 'e'));_math_node(children[0], base, font_size)
            limit = etree.SubElement(obj, _q(OMML_NS, 'lim'));_math_node(children[1], limit, font_size * .7)
        return
    if tag == 'mfenced':
        delim = etree.SubElement(parent, _q(OMML_NS, 'd'))
        props = etree.SubElement(delim, _q(OMML_NS, 'dPr'))
        etree.SubElement(props, _q(OMML_NS, 'begChr'), {_q(OMML_NS, 'val'): source.get('open', '(')})
        etree.SubElement(props, _q(OMML_NS, 'endChr'), {_q(OMML_NS, 'val'): source.get('close', ')')})
        expr = etree.SubElement(delim, _q(OMML_NS, 'e'))
        _math_children(source, expr, font_size)
        return
    if tag == 'mtable':
        matrix = etree.SubElement(parent, _q(OMML_NS, 'm'))
        etree.SubElement(matrix, _q(OMML_NS, 'mPr'))
        for row in children:
            if etree.QName(row).localname != 'mtr': raise ValueError('矩阵行格式无效')
            matrix_row = etree.SubElement(matrix, _q(OMML_NS, 'mr'))
            for cell in row:
                if etree.QName(cell).localname != 'mtd': raise ValueError('矩阵单元格式无效')
                expr = etree.SubElement(matrix_row, _q(OMML_NS, 'e'));_math_children(cell, expr, font_size)
        return
    raise ValueError('暂不支持公式结构：' + tag)


def append_omml(paragraph, latex: str, font_size: float = 22, align: str = 'center') -> None:
    """Append one PowerPoint-native, editable Office Math equation to a paragraph."""
    mathml = etree.fromstring(to_mathml(latex).encode('utf-8'))
    etree.register_namespace('a14', A14_NS)
    etree.register_namespace('m', OMML_NS)
    wrapper = etree.SubElement(paragraph._p, _q(A14_NS, 'm'))
    para = etree.SubElement(wrapper, _q(OMML_NS, 'oMathPara'))
    para_props = etree.SubElement(para, _q(OMML_NS, 'oMathParaPr'))
    etree.SubElement(para_props, _q(OMML_NS, 'jc'), {_q(OMML_NS, 'val'): align if align in ('left','center','right') else 'center'})
    equation = etree.SubElement(para, _q(OMML_NS, 'oMath'))
    _math_node(mathml, equation, max(8, min(96, float(font_size))))
