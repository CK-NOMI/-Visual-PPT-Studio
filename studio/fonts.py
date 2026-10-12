"""Discover locally installed, common CJK font families for PPTX rendering."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

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
