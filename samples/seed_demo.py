"""Create an offline sample with numbered design and clean images; no external network."""
import sys
from io import BytesIO
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image,ImageDraw,ImageFont
from studio.core import create_project,write_image,save_project,update_slide

def font(sz):
    for path in ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf','/System/Library/Fonts/Supplemental/Arial.ttf']:
        if Path(path).exists():return ImageFont.truetype(path,sz)
    return ImageFont.load_default()

p=create_project('NEURAL FUTURES','深度学习与产品创新',3,'dark','科研与产品团队')
titles=['NEURAL FUTURES','IDEA TO IMPACT','THE NEXT CHAPTER']
text=['AI research · Presentation demo','From data to decisions','Building what comes next']
for index,s in enumerate(p['slides']):
    update_slide(p,s['id'],{'title':titles[index],'body':text[index],
        'intent':'深色高级科技主视觉，右侧抽象流线，左侧文字区',
        'elements':[{'id':'title','kind':'text','box':[.07,.18,.55,.18],'text':titles[index],
                     'style':{'color':'FFFFFF','bold':True,'font':'Aptos Display','size':34}},
                    {'id':'body','kind':'text','box':[.075,.42,.48,.12],'text':text[index],
                     'style':{'color':'CBDAF2','bold':False,'font':'Aptos','size':18}}]})
p['locked']=True;save_project(p)
for i,s in enumerate(p['slides']):
  for role in ('design','clean'):
    im=Image.new('RGB',(1280,720),(11+i*3,20+i*2,46+i*3));d=ImageDraw.Draw(im,'RGBA')
    for k in range(12):
      d.arc((600-k*9,30+k*5,1390-k*9,740-k*5),30,300,fill=(80,135+k*6,245,135-k*5),width=2+k//3)
    d.ellipse((860,205,1170,515),fill=(55,123,235,63),outline=(107,186,255,210),width=4)
    if role=='design':
        d.text((85,160),titles[i],font=font(59),fill='#FFFFFF')
        d.text((90,310),text[i],font=font(32),fill='#CBDAF2')
    buf=BytesIO();im.convert('RGB').save(buf,'PNG')
    write_image(p,s['id'],role,buf.getvalue(),s['id']+'_'+role+'.png')
print('Created demonstration project:',p['id'])
print('Open http://127.0.0.1:8765 and select NEURAL FUTURES')
