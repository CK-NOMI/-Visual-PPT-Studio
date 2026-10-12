"""Discover locally installed, common CJK font families for PPTX rendering."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from functools import lru_cache
import hashlib
import logging

_BUNDLED_FONT_DIR = Path(__file__).parent / 'assets' / 'fonts'
_BUNDLED_FONTS = {
    'latin': {'Noto Sans': _BUNDLED_FONT_DIR / 'NotoSans-VF.ttf',
              'Noto Serif': _BUNDLED_FONT_DIR / 'NotoSerif-VF.ttf'},
    'east_asian': {'Noto Sans SC': _BUNDLED_FONT_DIR / 'NotoSansSC-VF.ttf',
                   'Noto Serif SC': _BUNDLED_FONT_DIR / 'NotoSerifSC-VF.ttf'},
}
_BUNDLED_CJK = _BUNDLED_FONTS['east_asian']['Noto Sans SC']

_CJK_FONTS = (
    ('Microsoft YaHei', ('microsoftyahei',)),
    ('SimSun', ('simsun',)),
    ('SimHei', ('simhei',)),
    ('KaiTi', ('kaiti',)),
    ('FangSong', ('fangsong',)),
    ('Songti SC', ('songtisc', 'songti sc')),
    ('HarmonyOS Sans SC', ('harmonyos_sans_sc', 'harmonyos-sans-sc')),
    ('Noto Sans SC', ('notosanssc', 'noto sans sc')),
    ('Noto Serif SC', ('notoserifsc', 'noto serif sc')),
    ('Hiragino Sans GB', ('hiraginosansgb', 'hiragino sans gb')),
    ('PingFang SC', ('pingfangsc', 'pingfang sc')),
    ('Source Han Sans SC', ('sourcehansanssc', 'source han sans sc')),
    ('Arial Unicode MS', ('arialunicode', 'arial unicode')),
)
_LATIN_FONTS = (
    ('Noto Sans', ('notosans', 'noto sans')),
    ('Noto Serif', ('notoserif', 'noto serif')),
    ('Arial', ('arial',)), ('Aptos', ('aptos',)), ('Calibri', ('calibri',)),
    ('Times New Roman', ('timesnewroman',)), ('Georgia', ('georgia',)),
    ('Helvetica Neue', ('helveticaneue',)), ('Liberation Sans', ('liberationsans',)),
)


def available_cjk_fonts() -> list[str]:
    """Return known family names confirmed by this host's font catalog."""
    catalog = ''
    if shutil.which('atsutil'):
        try:
            catalog = subprocess.run(['atsutil', 'fonts', '-list'], capture_output=True,
                                     text=True, timeout=5, check=False).stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not catalog and shutil.which('fc-list'):
        try:
            catalog = subprocess.run(['fc-list', ':', 'family'], capture_output=True,
                                     text=True, timeout=5, check=False).stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not catalog:
        font_dir = os.environ.get('WINDIR', '')
        if font_dir:
            try:
                catalog = ' '.join(os.listdir(os.path.join(font_dir, 'Fonts'))).lower()
            except OSError:
                pass
    normalized = re.sub(r'[^a-z0-9]', '', catalog)
    found = [family for family, aliases in _CJK_FONTS
             if any(re.sub(r'[^a-z0-9]', '', alias) in normalized for alias in aliases)]
    # Avoid an empty choice list on hosts with unusual font managers; this is a
    # fallback hint only, and the post-render OCR gate remains authoritative.
    if _BUNDLED_CJK.is_file() and 'Noto Sans SC' not in found:
        found.insert(0, 'Noto Sans SC')
    return found or ['Noto Sans SC', 'Microsoft YaHei']


def default_cjk_font() -> str:
    fonts = available_cjk_fonts()
    if _BUNDLED_CJK.is_file():
        return 'Noto Sans SC'
    if os.name == 'nt' and 'Microsoft YaHei' in fonts:
        return 'Microsoft YaHei'
    return fonts[0]


def _installed_families(catalog: str, families: tuple) -> list[str]:
    normalized = re.sub(r'[^a-z0-9]', '', catalog.lower())
    return [family for family, aliases in families
            if any(re.sub(r'[^a-z0-9]', '', alias) in normalized for alias in aliases)]


def font_choices() -> dict[str, list[str]]:
    """Bundled reproducible families plus other common families on this host."""
    catalog = ''
    command = ['atsutil', 'fonts', '-list'] if shutil.which('atsutil') else None
    if not command and shutil.which('fc-list'):
        command = ['fc-list', ':', 'family']
    if command:
        try:
            catalog = subprocess.run(command, capture_output=True, text=True,
                                     timeout=5, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    latin = list(_BUNDLED_FONTS['latin']) + _installed_families(catalog, _LATIN_FONTS)
    cjk = list(_BUNDLED_FONTS['east_asian']) + _installed_families(catalog, _CJK_FONTS)
    latin = list(dict.fromkeys(latin))
    cjk = list(dict.fromkeys(cjk))
    if _BUNDLED_CJK.is_file() and 'Noto Sans SC' not in cjk:
        cjk.insert(0, 'Noto Sans SC')
    if not latin:
        latin = ['Arial', 'Aptos', 'Calibri', 'Times New Roman', 'Georgia']
    if not cjk:
        cjk = ['Microsoft YaHei', 'SimSun', 'Noto Sans SC']
    return {'latin': latin, 'east_asian': cjk}


def _font_key(name):
    return re.sub(r'[\s_-]', '', str(name)).casefold()


@lru_cache(maxsize=1)
def font_registry() -> tuple[dict, ...]:
    """Index actual files and collection faces, never infer a face from its filename."""
    from fontTools.ttLib import TTFont, TTCollection
    logging.getLogger('fontTools.ttLib.tables._h_e_a_d').setLevel(logging.ERROR)
    directories = [_BUNDLED_FONT_DIR, Path.home()/'Library/Fonts', Path('/Library/Fonts'),
                   Path('/System/Library/Fonts'), Path('/usr/share/fonts'),
                   Path.home()/'.local/share/fonts']
    if os.environ.get('WINDIR'):
        directories.append(Path(os.environ['WINDIR'])/'Fonts')
    files = set()
    for directory in directories:
        if directory.is_dir():
            files.update(p for p in directory.rglob('*') if p.suffix.lower() in ('.ttf', '.otf', '.ttc', '.otc'))
    records = []
    for path in sorted(files):
        try:
            count = len(TTCollection(str(path), lazy=True).fonts) if path.suffix.lower() in ('.ttc', '.otc') else 1
            for index in range(count):
                with TTFont(str(path), fontNumber=index, lazy=True) as font:
                    def names(*ids):
                        return list(dict.fromkeys(n.toUnicode() for n in font['name'].names if n.nameID in ids))
                    families = names(16, 1)
                    if not families:
                        continue
                    axes = [{"tag": a.axisTag, "min": a.minValue, "default": a.defaultValue, "max": a.maxValue}
                            for a in font['fvar'].axes] if 'fvar' in font else []
                    weight = int(font['OS/2'].usWeightClass) if 'OS/2' in font else 400
                    style_names = names(17, 2)
                    records.append({'family': families[0], 'aliases': families,
                                    'path': str(path), 'index': index,
                                    'style': ', '.join(style_names), 'weight': weight,
                                    'italic': bool(font['head'].macStyle & 2), 'axes': axes,
                                    'version': (names(5) or ['unknown'])[0],
                                    'bundled': path.parent == _BUNDLED_FONT_DIR})
        except Exception:
            # Some platform fonts are encrypted or inaccessible to FreeType.
            # They are intentionally absent from the usable registry.
            continue
    return tuple(records)


@lru_cache(maxsize=512)
def resolve_font(family: str, text_class='latin', bold=False, italic=False) -> dict:
    """Return an explicit requested/actual mapping with a real FreeType file."""
    requested = str(family or ('Noto Sans SC' if text_class == 'cjk' else 'Noto Sans'))
    matches = [record for record in font_registry()
               if any(_font_key(requested) == _font_key(alias) for alias in record['aliases'])]
    fallback = not matches
    if fallback:
        actual = 'Noto Sans SC' if text_class == 'cjk' else 'Noto Sans'
        matches = [record for record in font_registry() if actual in record['aliases'] and record['bundled']]
    if not matches:
        raise ValueError('没有可测量的字体文件，请恢复 studio/assets/fonts 中的 Noto 字体')
    weight = 700 if bold else 400
    chosen = min(matches, key=lambda r: (r['italic'] != bool(italic),
                    0 if any(a['tag'] == 'wght' for a in r['axes']) else abs(r['weight']-weight),
                    not r['bundled']))
    result = dict(chosen)
    # Prefer the user's exact public family spelling if it matched a name record.
    result.update(requested_family=requested, actual_family=requested if not fallback else actual,
                  fallback=fallback, requested_weight=weight,
                  style_uncertain=chosen['italic'] != bool(italic) or
                      (not chosen['axes'] and abs(chosen['weight']-weight)>100),
                  file_identity=hashlib.sha256((chosen['path']+str(Path(chosen['path']).stat().st_mtime_ns)
                                               +str(chosen['index'])).encode()).hexdigest()[:16])
    return result


@lru_cache(maxsize=512)
def font_file_sha256(path):
    digest=hashlib.sha256()
    with open(path,'rb') as file:
        for block in iter(lambda:file.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=2048)
def measured_font(path: str, index: int, size: float, weight=400):
    """Measure at 16 pixels per point to avoid integer-pixel small-text errors."""
    from PIL import ImageFont
    font = ImageFont.truetype(path, max(1, round(float(size)*16)), index=index)
    try:
        axes = font.get_variation_axes()
        values = [min(a['maximum'], max(a['minimum'], weight))
                  if a['name'] == b'Weight' else a['default'] for a in axes]
        font.set_variation_by_axes(values)
    except (OSError, AttributeError):
        pass
    return font


def measure_text(text: str, size: float, family: str, text_class='latin', bold=False,
                 italic=False, tracking=0) -> dict:
    descriptor = resolve_font(family, text_class, bool(bold), bool(italic))
    descriptor={**descriptor,'file_sha256':font_file_sha256(descriptor['path']),
                'accessible_to_system':not descriptor['bundled'] or any(
                    not record['bundled'] and record['family']==descriptor['family'] for record in font_registry())}
    font = measured_font(descriptor['path'], descriptor['index'], float(size), descriptor['requested_weight'])
    missing = font_missing_characters(descriptor['path'], descriptor['index'], str(text))
    # getlength is the cursor advance, bbox is the visible ink; never substitute one for the other.
    bbox = [v/16 for v in font.getbbox(str(text), anchor='ls')]
    ascent, descent = (v/16 for v in font.getmetrics())
    import uharfbuzz as hb
    data=font_file_bytes(descriptor['path'])
    hbfont=hb.Font(hb.Face(data,descriptor['index']))
    hbfont.scale=(round(float(size)*64),round(float(size)*64))
    if descriptor['axes']:hbfont.set_variations({'wght':descriptor['requested_weight']})
    buffer=hb.Buffer();buffer.add_str(str(text));buffer.guess_segment_properties()
    hb.shape(hbfont,buffer,{'kern':True})
    cursor=0;ink=[]
    for info,pos in zip(buffer.glyph_infos,buffer.glyph_positions):
        extent=hbfont.get_glyph_extents(info.codepoint)
        if extent and (extent.width or extent.height):
            left=(cursor+pos.x_offset+extent.x_bearing)/64
            top=-(pos.y_offset+extent.y_bearing)/64
            ink.append([left,top,left+extent.width/64,top-extent.height/64])
        cursor+=pos.x_advance
    advance=cursor/64+max(0,len(str(text))-1)*float(tracking)
    if ink:bbox=[min(b[0] for b in ink),min(b[1] for b in ink),max(b[2] for b in ink),max(b[3] for b in ink)]
    return {'advance_pt': advance, 'ink_box_pt': bbox, 'ascent_pt': ascent,
            'descent_pt': descent, 'line_height_pt': ascent+descent,
            'font': descriptor, 'missing_characters': missing,
            'shaping': 'HarfBuzz advances/glyph extents + FreeType line metrics',
            'uncertain': bool(missing or descriptor['style_uncertain'])}


@lru_cache(maxsize=128)
def font_file_bytes(path):return Path(path).read_bytes()


@lru_cache(maxsize=4096)
def font_missing_characters(path, index, text):
    from fontTools.ttLib import TTFont
    with TTFont(path, fontNumber=index, lazy=True) as font:
        cmap = font.getBestCmap() or {}
        return ''.join(dict.fromkeys(c for c in text if not c.isspace() and ord(c) not in cmap))
