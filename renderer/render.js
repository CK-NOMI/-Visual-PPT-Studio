// Native PPTX renderer, written for this project. No model/API calls or image OCR.
const fs = require('fs');
const path = require('path');
let pptxgen;
try { pptxgen = require('pptxgenjs'); } catch(e) {
  console.error('Missing pptxgenjs; run: cd renderer && npm install'); process.exit(3);
}
if (process.argv.includes('--health')) { console.log('pptxgenjs available');process.exit(0); }
const [,, projectPath, outputPath, mode = 'editable'] = process.argv;
if (!projectPath || !outputPath) { console.error('Usage: node render.js PROJECT_JSON OUT.pptx [editable|raster]');process.exit(2); }
const project = JSON.parse(fs.readFileSync(projectPath, 'utf8'));
const pptx = new pptxgen();
pptx.layout = 'LAYOUT_WIDE';
pptx.author = 'Visual PPT Studio';
pptx.subject = 'Source-bound editable PowerPoint presentation';
pptx.title = project.title;
pptx.lang = 'zh-CN';
pptx.theme = {headFontFace:project.design.font||'Microsoft YaHei',bodyFontFace:project.design.font||'Microsoft YaHei',lang:'zh-CN'};
const SW=13.333333, SH=7.5;
function hex(x, fallback='000000') { return String(x||fallback).replace('#','').replace(/[^0-9a-f]/ig,'').slice(0,6)||fallback; }
function box(x) {const b=x.box;return {x:b[0]*SW,y:b[1]*SH,w:b[2]*SW,h:b[3]*SH};}
function fileFor(name) {
  if (!/^S\d{3}_(design|clean|asset_\d{2})\.(png|jpg|jpeg|webp)$/i.test(name||'')) throw new Error('Invalid image asset filename');
  const f=path.join(path.dirname(projectPath),'images',name);
  if (!fs.existsSync(f)) throw new Error('Asset missing: '+name);
  return f;
}
(async()=> {
for (const s of project.slides) {
  const slide=pptx.addSlide();
  slide.background={color:hex(s.background,project.design.bg)};
  if (mode==='raster') {
    const n=s.images?.design;
    if(n) slide.addImage({path:fileFor(n),x:0,y:0,w:SW,h:SH});
    else slide.addText('未上传 '+s.id+'_design.png',{x:1,y:3,w:11,h:1,fontSize:28,color:'AAAAAA',align:'center'});
    continue;
  }
  // In editable mode we NEVER place a design image with embedded text as the background.
  if (s.images?.clean) slide.addImage({path:fileFor(s.images.clean),x:0,y:0,w:SW,h:SH});
  for(const e of (s.elements||[])) {
    const b=box(e); const st=e.style||{};
    const fontFace=st.font||project.design.font||'Microsoft YaHei';
    const common={...b,rotate:Number(st.rotate||0), transparency:0};
    if(e.kind==='text') {
      slide.addText(String(e.text||''),{...b,fontFace,fontSize:Number(st.size||18),
        bold:!!st.bold,color:hex(st.color,project.design.ink),
        margin:0,breakLine:false,vertAlign:'top',valign:'top',
        align:st.align||'left',transparency:Number(st.opacity||0),
        lineSpacingMultiple:1.05,paraSpaceAfterPt:0});
    } else if(e.kind==='rect' || e.kind==='ellipse') {
      const t=e.kind==='ellipse'?pptx.ShapeType.ellipse:pptx.ShapeType.rect;
      slide.addShape(t,{...b,rectRadius:0,
         fill:{color:hex(st.fill,project.design.accent),transparency:Number(st.transparency||0)},
         line:{color:hex(st.stroke,st.fill||project.design.accent),width:Number(st.strokeWidth||0)}});
    } else if(e.kind==='line') {
      slide.addShape(pptx.ShapeType.line,{...b,
         line:{color:hex(st.stroke,project.design.accent),width:Number(st.strokeWidth||2),beginArrowType:st.beginArrow||'none',endArrowType:st.arrow?'triangle':'none'}});
    } else if(e.kind==='image') {
      const imgPath=fileFor(e.asset);
      const fit=st.fit==='contain'?'contain':'cover';
      let opts={path:imgPath,...b};
      slide.addImage(opts);
    }
  }
  slide.addNotes?.(`Slide ID ${s.id}. Original content source: project.json. ${s.notes||''}`);
}
await pptx.writeFile({fileName:outputPath});
console.log(JSON.stringify({file:outputPath,slides:project.slides.length,mode}));
})().catch(e=>{console.error(e.stack||e);process.exit(1)});
