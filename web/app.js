/* Visual PPT Studio browser interface. No frameworks, external CDN or tracking. */
(() => {
'use strict';
const $ = id => document.getElementById(id);
const state={project:null,projects:[],sid:'S001',tab:'plan',mode:'editable',elements:[],selected:null,masks:[],drag:null,proposal:null,lastReport:null,prompts:null,aiImagePrompts:{},pairReports:{},calibration:null,calibrationDeck:[],formulaMathML:{},formulaJobs:{},labRegions:[],labCrop:null,labAvailable:null,autoCandidateSid:null,autoCandidateKind:null,autoStageRetry:null,reconstructionPages:[],secondStageTransitioning:false,uploadPreviewUrl:null,uploadUndo:null,optimizationRun:null};
const esc = s => String(s??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const csshex = c => '#'+String(c||'000000').replace('#','');
const CJK_FONT_FAMILIES=new Set(['Noto Sans SC','Noto Serif SC','Microsoft YaHei','SimSun','SimHei','KaiTi','FangSong','Songti SC','HarmonyOS Sans SC','Hiragino Sans GB','PingFang SC','Arial Unicode MS']);
const EAST_ASIAN_CHAR=/[\u2E80-\u9FFF\uAC00-\uD7AF\u3040-\u30FF\uFF00-\uFFEF]/u;
function textFontRuns(text,latin,east){return (String(text).match(/[\u2E80-\u9FFF\uAC00-\uD7AF\u3040-\u30FF\uFF00-\uFFEF]+|[^\u2E80-\u9FFF\uAC00-\uD7AF\u3040-\u30FF\uFF00-\uFFEF]+/gu)||[]).map(part=>{const family=EAST_ASIAN_CHAR.test(part)?east:latin;return family&&family!==latin?`<span style="font-family:&quot;${esc(String(family).replace(/["\\]/g,''))}&quot;">${esc(part)}</span>`:esc(part)}).join('')}
const json = x => JSON.stringify(x,null,2);
let noticeTimer;
function toast(message,isError=false){let d=$('notice');d.textContent=message;d.className='notice'+(isError?' error':'');clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>d.classList.add('hidden'),6500)}
const api=(url,options={})=>window.StudioAPI.request(url,options);
const post=(url,body={})=>window.StudioAPI.post(url,body);
const put=(url,body)=>window.StudioAPI.put(url,body);
async function run(fn){try{await fn()}catch(e){console.error(e);toast(e.message||String(e),true)}}
function renderOptimizationLog(){const target=$('calibrationDeckResults');if(!target)return;const entries=state.optimizationRun?.log||[],suggestions=state.calibrationDeck||[],memory=state.optimizationRun?.memory;const recentMemory=(memory?.history||[]).slice(-4).reverse();const memoryDetails=recentMemory.length?`<details class="deck-review-row"><summary><strong>最近逐轮完整记录（全部差异、DeepSeek反馈、候选参数、实测结果）</strong></summary>${recentMemory.map(h=>`<p><b>${esc(h.slide_id)} 第${h.round}轮 · ${esc(h.outcome)} · ${h.before??'—'}→${h.after??'—'}（${h.delta==null?'—':`${h.delta>=0?'+':''}${h.delta}`}）${h.candidate_applied?' · 已应用候选':''}</b><br>原稿/实渲染差异：${esc(JSON.stringify(h.differences_before||[]))}<br>DeepSeek反馈：${esc((h.issues||[]).join('；')||'无')}<br>本轮参数：${esc(JSON.stringify(h.corrections||[])||'无')}</p><p>本轮渲染后剩余差异：${esc(JSON.stringify(h.differences_after||[])||'未记录')}</p>`).join('')}</details>`:'';const memoryHtml=memory?`<div class="deck-review-row"><strong>跨轮经验记忆</strong><span>已记录 ${memory.history?.length||0} 次实测；${memory.playbook?.length||0} 条经提升验证的提示词经验（版本 ${memory.playbook?.length||0}）。未提升的候选/反馈也会留档并供下一轮参考。</span></div>${memoryDetails}`:'';const logHtml=entries.map(x=>`<div class="deck-review-row ${x.kind==='error'?'warn':''}"><strong>${esc(x.title)}</strong><span>${esc(x.message)}</span></div>`).join('');const suggestionHtml=suggestions.map(x=>x.ok?`<div class="deck-review-row"><strong>${esc(x.slide_id)} · DeepSeek诊断 ${x.result.score}/100</strong><span>${x.autoApplied?`已应用候选 ${x.result.corrections.length} 项`:(x.applyError?`应用失败：${esc(x.applyError)}`:x.result.corrections.length?`候选调整 ${x.result.corrections.length} 个文字框`:'未建议调整')}</span><button class="btn small" data-calibration-slide="${esc(x.slide_id)}">查看建议</button></div>`:`<div class="deck-review-row warn"><strong>${esc(x.slide_id)} · 监督失败</strong><span>${esc(x.error)}</span></div>`).join('');target.innerHTML=memoryHtml+logHtml+suggestionHtml||'<p class="hint">点击「启动样式诊断」后，这里会逐轮显示真实渲染分数和Agent动作。</p>';target.querySelectorAll('[data-calibration-slide]').forEach(btn=>btn.onclick=()=>{const id=btn.dataset.calibrationSlide,s=state.project.slides.find(y=>y.id===id),entry=state.calibrationDeck.find(y=>y.slide_id===id&&y.ok);if(!s||!entry)return;state.sid=id;state.elements=JSON.parse(JSON.stringify(s.elements));state.elsSid=id;state.selected=state.elements[0]?.id||null;showCalibration(entry.result);assignTab('qa');toast(`已载入 ${id} 的校准建议；系统会以真实渲染分判断候选是否保留`)})}
function logOptimization(title,message,kind='info'){if(!state.optimizationRun)state.optimizationRun={best:{},stopped:{},rounds:0,log:[]};state.optimizationRun.log=state.optimizationRun.log||[];state.optimizationRun.log.push({title,message,kind});renderOptimizationLog()}
function pid(){if(!state.project)throw Error('请先创建或选择项目');return state.project.id}
function uploadUndoKey(projectId){return `visual-ppt-studio:upload-undo:${projectId}`}
function slide(){if(!state.project)return null;return state.project.slides.find(s=>s.id===state.sid)||state.project.slides[0]}
function murl(name){return name?`/media/${pid()}/${encodeURIComponent(name)}?rev=${state.project.revision}`:''}
function promptList(){return state.prompts?.prompts||[]}
function aiReq(){return {api_key:$('apiKey').value.trim()||null,model:$('modelName').value.trim()||'deepseek-flash'}}
const loadedFontFaces=new Map();
state.fontStatus={};
async function ensureProjectFonts(project){
 const faces=project.slides.flatMap(s=>(s.elements||[]).map(e=>e.text_layout?.font)).filter(Boolean);
 await Promise.all([...new Map(faces.map(f=>[`${f.file_identity}:${f.requested_weight}`,f])).values()].map(async f=>{
  const key=`${f.file_identity}:${f.requested_weight}`,family=`StudioFace_${f.file_identity}`;
  if(!loadedFontFaces.has(key))loadedFontFaces.set(key,(async()=>{
   try{const face=new FontFace(family,`url(/api/projects/${project.id}/fonts/${f.file_identity}.ttf)`,{weight:String(f.requested_weight||400),style:f.italic?'italic':'normal'});await face.load();document.fonts.add(face);return {family,status:face.status,source:f.path,file_identity:f.file_identity,fallback:f.fallback,requested:f.requested_family,actual:f.actual_family};}
   catch(error){return {family:f.actual_family,status:'error',error:error.message,fallback:true};}
  })());
  state.fontStatus[key]=await loadedFontFaces.get(key);
 }));
 await document.fonts.ready;
}
function browserFontFamily(element,fallback){const f=element.text_layout?.font;return f&&state.fontStatus[`${f.file_identity}:${f.requested_weight}`]?.status==='loaded'?`StudioFace_${f.file_identity}`:fallback;}
function alignGlyphBaselines(){
 const stage=$('stage'),rect=stage.getBoundingClientRect();if(!rect.width)return;
 for(const e of state.elements){const layout=e.text_layout;if(e.kind!=='text'||!layout)continue;
  const node=[...stage.querySelectorAll('[data-el]')].find(n=>n.dataset.el===e.id),line=node?.querySelector('.glyph-line'),marker=node?.querySelector('.baseline-marker');if(!line||!marker)continue;
  line.style.transform='';const target=rect.top+layout.baseline_pt*rect.width/960;
  line.style.transform=`translateY(${target-marker.getBoundingClientRect().top}px)`;
 }
}
function renderProjects(){
 $('projectList').innerHTML=window.StudioComponents.AppShell.projectItems(state.projects,state.project?.id);
 $('projectList').querySelectorAll('[data-pid]').forEach(x=>x.addEventListener('click',()=>run(()=>loadProject(x.dataset.pid))));
 $('batchProjects').innerHTML=state.projects.map(p=>`<div class="checkbox-row"><input type="checkbox" class="batch-check" value="${p.id}" ${p.locked?'checked':''}><label>${esc(p.title)} <small>(${p.pages}页，${p.locked?'已锁定':'未锁定'})</small></label></div>`).join('')||'暂无项目';
}
async function refreshProjects(){state.projects=await api('/api/projects');renderProjects()}
let projectLoadSerial=0;
function setProjectLoading(value){state.projectLoading=value;document.body.dataset.projectLoading=String(value);$('saveElements').disabled=value;$('saveSlide').disabled=value;$('autoRebuildPage').disabled=value;}
async function loadProject(id,preferredSid=null){
 const serial=++projectLoadSerial;setProjectLoading(true);
 try{const project=await api('/api/projects/'+id);if(serial!==projectLoadSerial)return;
  await ensureProjectFonts(project);if(serial!==projectLoadSerial)return;
  state.project=project;state.sid=project.slides.some(s=>s.id===preferredSid)?preferredSid:project.slides[0].id;
  try{state.uploadUndo=JSON.parse(localStorage.getItem(uploadUndoKey(id))||'null')}catch{state.uploadUndo=null}
  state.prompts=null;state.aiImagePrompts={};state.pairReports={};state.calibration=null;state.lastReport=null;state.reconstructionPages=[];
  try{state.lastReport=await api(`/build/${id}/report_editable.json`)}catch{}
  if(serial!==projectLoadSerial)return;
  state.selected=null;state.masks=[];state.elements=[];state.elsSid=null;state.labRegions=[];$('autoRebuildResult').textContent='等待本页处理，可打开手动调整读取识别候选。';
  await refreshProjects();if(serial!==projectLoadSerial)return;
  renderEverything();await refreshReconstructionProgress();if(state.tab==='qa'){try{sessionStorage.setItem('visualPPTStudio.qaProject',id)}catch{}if(!repairAutoPromise)run(restoreRepairCandidate);}
 }finally{if(serial===projectLoadSerial){setProjectLoading(false);renderAgentReadiness();}}
}
function renderEverything(){
 let p=state.project;if(!p){$('projectTitle').textContent='可编辑 AI PPT 生产台';return;}
 window.StudioComponents.AppShell.setProjectContext(p);
 $('planStats').innerHTML=stat('页面',p.slides.length)+stat('完整设计稿',p.slides.filter(s=>s.images.design).length)+stat('无文字背景',p.slides.filter(s=>s.images.clean).length)+stat('规划状态',p.locked?'已锁定':'可编辑');
 $('uploadStats').innerHTML=stat('已规划',p.slides.length)+stat('设计图',p.slides.filter(s=>s.images.design).length)+stat('无字底图',p.slides.filter(s=>s.images.clean).length)+stat('素材',p.slides.reduce((n,s)=>n+Object.keys(s.images).filter(r=>r.startsWith('asset_')).length,0));
 $('planList').innerHTML=p.slides.map(s=>`<button class="plan-entry ${s.id===state.sid?'active':''}" data-sid="${s.id}"><b>${s.id}</b><div><strong>${esc(s.title||`第${s.number}页 · 待规划`)}</strong><small>${esc(s.intent.slice(0,80))} · ${s.images.design?'◆设计稿':''}${s.images.clean?' ◆底图':''}</small></div></button>`).join('');
 $('planList').querySelectorAll('[data-sid]').forEach(x=>x.addEventListener('click',()=>chooseSlide(x.dataset.sid)));
 const s=slide();$('planEditorTitle').textContent=`${s.id} · 内容源`; $('slideTitle').value=s.title;$('slideBody').value=s.body;$('slideIntent').value=s.intent;$('slideNotes').value=s.notes||'';
 $('colorBg').value=p.design.bg;$('colorInk').value=p.design.ink;$('colorAccent').value=p.design.accent;$('colorMuted').value=p.design.muted;$('designFont').value=p.design.font;
 $('swatches').innerHTML=['bg','ink','accent','muted'].map(k=>`<div title="${k}: #${p.design[k]}" style="background:#${p.design[k]}"></div>`).join('');
 $('slotSlide').innerHTML=p.slides.map(s=>`<option value="${s.id}" ${s.id===state.sid?'selected':''}>${s.id} · ${esc(s.title||`第${s.number}页 · 待规划`)}</option>`).join('');
 $('wpsRenderSlide').innerHTML=p.slides.map(s=>`<option value="${s.id}" ${s.id===state.sid?'selected':''}>${s.id}</option>`).join('');renderAssets();renderCurrentUpload();renderAgentReadiness();renderPrompts();renderEditor();renderQAImages();renderStaleExportStatus();renderPairReport();
}
const stat=(label,value)=>window.StudioComponents.StatCard.render(label,value);
function chooseSlide(sid){if(state.projectLoading){toast('项目正在加载，请稍后再切页');return;}if(state.autoCandidateSid&&state.autoCandidateSid!==sid){toast(`请先确认并保存 ${state.autoCandidateSid} 的 Agent 候选，再切换页面`,true);$('slotSlide').value=state.sid;return}state.sid=sid;state.elements=[];state.selected=null;state.masks=[];state.labRegions=[];renderEverything();if(state.tab==='upload'){$('autoRebuildResult').textContent='正在读取本页识别候选…';run(restoreAgentCandidatePreview)}if(state.tab==='lab')run(labRefresh)}
function renderAssets(){
 const p=state.project;if(!p)return;
 $('assetTable').innerHTML='<div class="asset-row head"><div>页面</div><div>完整设计稿</div><div>无文字底图</div><div>独立素材</div></div>'+p.slides.map(s=>{
 const art=Object.keys(s.images).filter(a=>a.startsWith('asset_'));
 return `<div class="asset-row"><strong>${s.id}</strong><div>${s.images.design?`<img src="${murl(s.images.design)}">`: '<span class="warn">未上传</span>'}</div><div>${s.images.clean?`<img src="${murl(s.images.clean)}">`: '<span class="warn">未上传</span>'}</div><div>${art.length?art.map(a=>`<a href="${murl(s.images[a])}" target="_blank">${a}</a>`).join('<br>'):'<span class="muted">—</span>'}</div></div>`
 }).join('');
}
function roleLabel(role){return ({design:'完整设计稿',clean:'无字背景图',asset_01:'透明素材一',asset_02:'透明素材二'})[role]||role}
function renderCurrentUpload(){
 if(!state.project)return;
 const f=$('slotFile').files?.[0],slot=$('slotPreview'),label=$('slotPickerLabel'),btn=$('uploadOne');
 if(state.uploadPreviewUrl){URL.revokeObjectURL(state.uploadPreviewUrl);state.uploadPreviewUrl=null}
 if(f){state.uploadPreviewUrl=URL.createObjectURL(f);slot.classList.remove('hidden');slot.innerHTML=`<img src="${state.uploadPreviewUrl}" alt="待上传图片预览"><div class="upload-preview-meta"><strong>待上传预览 · ${esc(roleLabel($('slotRole').value))}</strong><span>${esc(f.name)} · ${(f.size/1024/1024).toFixed(2)} MB</span><div class="actions"><button class="btn small" id="replaceSlotFile" type="button">换一张</button><button class="btn small danger" id="clearSlotFile" type="button">清除</button></div></div>`;
  $('replaceSlotFile').onclick=()=> $('slotFile').click();$('clearSlotFile').onclick=()=>{ $('slotFile').value='';renderCurrentUpload()};
  label.innerHTML='图片已选中 · 再次点击可更换<br><small>核对预览和图片类型后，点击下方“上传图片”保存</small>';btn.disabled=false;
 }else{slot.classList.add('hidden');slot.innerHTML='';label.innerHTML='选择或拖入一张图片<br><small>PNG / JPG / WEBP · 原文件名不限，自动关联当前页与图片类型</small>';btn.disabled=true}
 const s=slide(),role=$('slotRole').value,name=s?.images?.[role],meta=s?.image_metadata?.[role],saved=$('savedSlotImage');
 if(name){const undo=state.uploadUndo?.slide_id===s.id&&state.uploadUndo?.role===role?`<button class="btn small" id="undoRemoveUpload" type="button">撤回移除</button>`:'';
  saved.innerHTML=`<div class="saved-upload-card"><img src="${murl(name)}" alt="${esc(roleLabel(role))}预览"><div class="saved-upload-meta"><strong>当前页已保存：${esc(roleLabel(role))}</strong><span>系统文件名：${esc(name)}</span><span>原文件名：${esc(meta?.original_filename||'（记录中无原文件名）')}</span><div class="actions"><button class="btn small danger" id="removeSavedUpload" type="button">移除这张图</button>${undo}</div></div></div>`;
  $('removeSavedUpload').onclick=()=>run(removeCurrentUpload);if($('undoRemoveUpload'))$('undoRemoveUpload').onclick=()=>run(undoRemoveUpload);
 }else{const undo=state.uploadUndo?.slide_id===s?.id&&state.uploadUndo?.role===role?`<div class="undo-upload"><span>刚移除：${esc(state.uploadUndo.filename)}</span><button class="btn small" id="undoRemoveUpload" type="button">撤回移除</button></div>`:'';saved.innerHTML=undo;if($('undoRemoveUpload'))$('undoRemoveUpload').onclick=()=>run(undoRemoveUpload)}
}
function renderAgentReadiness(){
 if(!state.project)return;const s=slide(),ready=!!s?.images?.design&&cleanPairCurrent(s),lines=[];
 if(!s?.images?.design)lines.push(`${s.id} 尚未上传完整设计稿`);
 else if(!s.images.clean)lines.push(`${s.id} 尚未上传无字背景图`);
 else if(!cleanPairCurrent(s))lines.push(`${s.id} 的无字背景图与当前设计稿未配对或已过期，请重新上传该页 clean 图`);
 else lines.push(`${s.id} 的完整设计稿与无字背景图已就绪，可运行 Agent`);
 const status=state.reconstructionPages.find(x=>x.slide_id===s?.id);if(status?.confirmed)lines.push('本页第二步已确认保存');
 $('agentReadiness').textContent=lines.join('；');$('agentReadiness').className='agent-readiness '+(ready?'good':'warn');$('autoRebuildPage').disabled=state.projectLoading||!ready;
 renderReconstructionProgress();
}
function renderReconstructionProgress(){
 const target=$('reconstructionProgress'),finish=$('finishReconstruction');if(!target||!finish||!state.project)return;
 const pages=state.reconstructionPages||[],done=pages.filter(x=>x.ready&&x.confirmed).length,allReady=pages.length>0&&pages.every(x=>x.ready),allDone=allReady&&pages.every(x=>x.confirmed);
 target.innerHTML=`<strong>第二步整套进度：${done} / ${pages.length} 页已确认</strong><div class="reconstruction-page-list">${pages.map(p=>{const slide=state.project.slides.find(s=>s.id===p.slide_id);const label=!p.ready?(!p.has_design?'缺完整设计稿':!p.has_clean?'缺无字底图':'design / clean 未配对'):p.confirmed?(p.legacy?'已保存（历史结果）':'已确认保存'):'待运行并确认';return `<button type="button" class="reconstruction-page ${p.slide_id===state.sid?'active':''} ${p.confirmed?'done':p.ready?'pending':'blocked'}" data-reconstruction-slide="${esc(p.slide_id)}"><b>${esc(p.slide_id)}</b><span>${esc(slide?.title||'未命名页面')}</span><small>${esc(label)}</small></button>`}).join('')}</div><p class="hint">每页单独运行 Agent 并确认保存；不同页面的 OCR 区域和完成记录分别保存。当前页确认后仍留在第二步，全部页面都就绪并确认后，才会进入第三步导出整套 PPTX。</p>`;
 target.querySelectorAll('[data-reconstruction-slide]').forEach(button=>button.onclick=()=>chooseSlide(button.dataset.reconstructionSlide));
 finish.disabled=!allDone||state.secondStageTransitioning;finish.textContent=state.autoStageRetry?'重试第三步校准':state.secondStageTransitioning?'正在导出整套并进入第三步…':allDone?'全部页面已完成 · 进入第三步':`全部页面完成后进入第三步（${done}/${pages.length}）`;
}
async function refreshReconstructionProgress(){if(!state.project)return;const id=state.project.id;const data=await api(`/api/projects/${id}/reconstruction-progress`);if(state.project?.id!==id)return;state.reconstructionPages=data.pages||[];renderAgentReadiness()}
function renderPrompts(){
 const p=state.project;if(!p)return;
 const s=slide(),d=p.design;
 const copy=s.title||s.body?`必须把已填写文案作为可读文字呈现在完整设计稿中。标题：${s.title||'未单独指定'}。正文：${s.body||'未单独指定'}。不要删去已提供文字。`:`本页文案尚未规划，请根据项目主题“${p.topic}”和用途“${s.intent}”生成合适标题与精简正文，直接呈现在完整设计稿中；不要出现占位词或编造精确数据。`;
 const common=`制作专业演示文稿第 ${s.id.slice(1)} 页，画幅16:9，建议1920×1080或更高。视觉风格：${p.style}，主色 #${d.accent}，背景 #${d.bg}，文字色 #${d.ink}。项目主题：${p.topic}。页面用途：${s.intent}。${copy}版式专业、层级清晰、美观且方便转为可编辑PPT。重要：此为含文字的完整设计稿；无文字要求仅适用于 clean 底图阶段。`;
 $('promptNumber').textContent=s.id;
 const ai=state.aiImagePrompts[s.id];
 $('promptDesign').value=ai?.design_prompt||`【${s.id} 设计稿】${common}\n只生成一页完整视觉设计稿，不要输出多页拼图。保存为 ${s.id}_design.png。`;
 $('promptClean').value=ai?.clean_prompt||`【${s.id} 无文字背景】请在 Image 编辑模式中直接编辑输入的 ${s.id}_design.png，不要从零生成或重绘。保持光影、装饰、配图、比例和构图，移除全部标题、正文、数字、标签、页码、文字阴影，背景自然衔接。不允许新增任何字符。保存为 ${s.id}_clean.png。`;
 $('promptAsset').value=`【${s.id} 独立素材】如有复杂插画，请基于原设计稿单独生成背景透明且不含文字的 PNG，保存为 ${s.id}_asset_01.png。可选。`;
}
function selectElement(id){state.selected=id;renderEditor()}
function selected(){return state.elements.find(e=>e.id===state.selected)}
function renderEditor(){
 const p=state.project;if(!p)return;const s=slide();
 if(!state.elements.length||state.elsSid!==s.id){state.elements=JSON.parse(JSON.stringify(s.elements));state.elsSid=s.id;state.selected=state.elements[0]?.id||null;}
 $('stageLabel').textContent=s.id+' · '+state.mode;
 const textObjects=state.elements.filter(e=>['text','formula'].includes(e.kind)&&String(e.text||'').trim());
 const uncertain=textObjects.filter(e=>!e.text_layout||e.text_layout.uncertain||state.fontStatus[`${e.text_layout?.font?.file_identity}:${e.text_layout?.font?.requested_weight}`]?.status!=='loaded');
 $('browserFontStatus').textContent=uncertain.length?`字体/布局需复核：${uncertain.map(e=>`${e.id}：${e.text_layout?.font?.requested_family||e.style?.font||'未测量'} → ${e.text_layout?.font?.actual_family||'未确定'}，${e.text_layout?.uncertain?'度量不确定':'字体未明确加载'}`).join('；')}`:`本页 ${textObjects.length} 个文字/公式对象使用已加载的明确字体文件；网页与PPTX共享已保存布局。`;

 $('editorModes').querySelectorAll('[data-mode]').forEach(x=>x.classList.toggle('active',x.dataset.mode===state.mode));
 let bg=state.mode==='design'?s.images.design:(state.mode==='mask'?s.images.design:s.images.clean);
 $('stageBg').style.backgroundImage=bg?`url("${murl(bg)}")`:'none';
 $('stageBg').style.backgroundColor=bg?'transparent':csshex(s.background);
 $('stageElements').style.display=state.mode==='editable'?'block':'none';
 $('stageMasks').style.display=state.mode==='mask'?'block':'none';
 $('stage').style.cursor=state.mode==='mask'?'crosshair':'default';
 $('refOverlay').src=s.images.design?murl(s.images.design):'';
 $('refOverlay').style.opacity=(state.mode==='editable'&&s.images.design)?String(Number($('referenceOpacity').value)/100):'0';
 $('stageElements').innerHTML=state.elements.map(e=>{
   const [x,y,w,h]=e.box;const st=e.style||{};
   const pos=`left:${x*100}%;top:${y*100}%;width:${w*100}%;height:${h*100}%;`;
   if(e.kind==='text'){const latin=browserFontFamily(e,st.font||p.design.font||'Arial'),east=browserFontFamily(e,st.eastAsianFont||'Noto Sans SC'),baseSize=Math.max(4,Number(st.size||16)),fittedSize=baseSize,paras=String(e.text||'').split('\n').map((line,i)=>`<span class="glyph-line" style="display:block;white-space:pre;margin:${i?Number(st.paragraph_space_before_pt||0)/9.6:0}cqw 0 ${Number(st.paragraph_space_after_pt||0)/9.6}cqw">${textFontRuns(line,latin,east)||'&nbsp;'}<span class="baseline-marker" style="display:inline-block;width:0;height:0;vertical-align:baseline"></span></span>`).join('');return `<div class="stage-el ${e.id===state.selected?'selected':''}" data-el="${esc(e.id)}" style="${pos}display:flex;flex-direction:column;justify-content:${({top:'flex-start',middle:'center',bottom:'flex-end'})[st.vertical_align||'top']};padding:${Number(st.margin_top_pt||0)/9.6}cqw ${Number(st.margin_right_pt||0)/9.6}cqw ${Number(st.margin_bottom_pt||0)/9.6}cqw ${Number(st.margin_left_pt||0)/9.6}cqw;font-size:${fittedSize/9.6}cqw;color:${csshex(st.color||p.design.ink)};font-weight:${st.bold?'700':'400'};font-style:${st.italic?'italic':'normal'};font-family:&quot;${esc(String(latin).replace(/["\\]/g,''))}&quot;;letter-spacing:${Number(st.char_spacing_pt||0)/9.6}cqw;line-height:${Number(st.line_spacing||1.05)};text-align:${st.align||'left'};white-space:pre;">${paras}</div>`;}
   if(e.kind==='formula')return `<div class="stage-el equation ${e.id===state.selected?'selected':''}" data-el="${esc(e.id)}" style="${pos}font-size:${Math.max(8,Number(st.size||22))/9.6}cqw;color:${csshex(st.color||p.design.ink)};" title="LaTeX 源码可编辑"><span class="formula-preview">${esc(e.text)}</span></div>`;
   if(e.kind==='image')return `<img class="stage-el image ${e.id===state.selected?'selected':''}" data-el="${esc(e.id)}" src="${murl(e.asset)}" style="${pos}">`;
   if(e.kind==='rect'||e.kind==='ellipse')return `<div class="stage-el shape ${e.id===state.selected?'selected':''}" data-el="${esc(e.id)}" style="${pos}background:${csshex(st.fill||p.design.accent)};border-radius:${e.kind==='ellipse'?'50%':'0'};"></div>`;
   if(e.kind==='line')return `<div class="stage-el ${st.arrow?'arrow':''} ${e.id===state.selected?'selected':''}" data-el="${esc(e.id)}" style="${pos}color:${csshex(st.stroke||p.design.accent)};border-top:${st.strokeWidth||2}px solid ${csshex(st.stroke||p.design.accent)};"></div>`;
   return '';
 }).join('');
 state.elements.filter(e=>e.kind==='formula').forEach(e=>renderFormulaPreview(e));
 requestAnimationFrame(alignGlyphBaselines);
 $('stageMasks').innerHTML=state.masks.map(r=>`<div class="stage-mask" style="left:${r[0]*100}%;top:${r[1]*100}%;width:${r[2]*100}%;height:${r[3]*100}%"></div>`).join('');
 $('stageElements').querySelectorAll('[data-el]').forEach(el=>{
   el.addEventListener('pointerdown',event=>{event.stopPropagation();if(state.mode!=='editable')return;
     state.selected=el.dataset.el;const e=selected();const rect=$('stage').getBoundingClientRect();
     state.drag={id:e.id,startX:event.clientX,startY:event.clientY,box:[...e.box],width:rect.width,height:rect.height};
     el.setPointerCapture?.(event.pointerId);renderProperties();
     $('stageElements').querySelectorAll('[data-el]').forEach(x=>x.classList.toggle('selected',x.dataset.el===state.selected));
   });
   el.addEventListener('dblclick',()=>{const e=selected();if(e&&e.kind==='text'){const t=prompt('编辑可编辑文字：',e.text);if(t!==null){e.text=t;run(layoutCurrentElements);}}});
 });
 $('elementSelect').innerHTML=state.elements.map(e=>`<option value="${esc(e.id)}" ${e.id===state.selected?'selected':''}>${esc(e.id)} (${e.kind})</option>`).join('');
 $('elementAsset').innerHTML=Object.keys(s.images).filter(k=>k.startsWith('asset_')).map(k=>`<option value="${s.images[k]}">${esc(s.images[k])}</option>`).join('')||'<option value="">请先上传独立素材</option>';
 renderProperties();
}
function renderProperties(){
 const e=selected();if(!e)return;
 $('elementSelect').value=e.id;$('elementText').value=e.text||'';
 $('elementTextLabel').textContent=e.kind==='formula'?'LaTeX 公式源码':'对象文字';$('formulaHint').classList.toggle('hidden',e.kind!=='formula');
 ['X','Y','W','H'].forEach((n,i)=>$('el'+n).value=Number(e.box[i]).toFixed(9));
 $('elSize').value=e.style?.size||18;$('elColor').value=e.style?.color||e.style?.fill||state.project.design.ink;
 $('elFont').value=e.style?.font||'';$('elBold').checked=!!e.style?.bold;$('elItalic').checked=!!e.style?.italic;$('elCharSpacing').value=e.style?.char_spacing_pt??0;$('elLineSpacing').value=e.style?.line_spacing??1.05;
 $('elMarginLeft').value=e.style?.margin_left_pt??0;$('elMarginRight').value=e.style?.margin_right_pt??0;$('elMarginTop').value=e.style?.margin_top_pt??0;$('elMarginBottom').value=e.style?.margin_bottom_pt??0;$('elParagraphBefore').value=e.style?.paragraph_space_before_pt??0;$('elParagraphAfter').value=e.style?.paragraph_space_after_pt??0;$('elVerticalAlign').value=e.style?.vertical_align||'top';
 if(e.kind==='image')$('elementAsset').value=e.asset;
}
async function renderFormulaPreview(e){
 const findNode=()=>[...document.querySelectorAll('[data-el]')].find(n=>n.dataset.el===e.id);
 try{
  let markup=state.formulaMathML[e.text];
  if(!markup){state.formulaJobs[e.text] ||= post('/api/formula/preview',{latex:e.text}).then(r=>(state.formulaMathML[e.text]=r.mathml)).finally(()=>delete state.formulaJobs[e.text]);markup=await state.formulaJobs[e.text];}
  const node=findNode();if(!node)return;const doc=new DOMParser().parseFromString(markup,'application/xml');
  if(doc.querySelector('parsererror'))throw Error('MathML 预览格式异常');node.replaceChildren(document.importNode(doc.documentElement,true));
 }catch(err){const node=findNode();if(node){node.textContent=e.text;node.title='公式预览失败：'+err.message}}
}
function renderQAImages(){
 if(!state.project)return;
 const report=state.lastReport,previews=report?.previews||[],stale=!!report&&(report.project_revision!==state.project.revision||!report.artifact_sha256||report.artifact_valid_current===false);
 const r=repairState,sameRevision=r?.project_id===state.project.id&&(r.base_revision===state.project.revision||r.committed_revision===state.project.revision);
 const browserReady=sameRevision&&r.comparison&&r.validation_complete!==false&&['validated_pending_commit','requires_review','committed','checked_current_project'].includes(r.status);
 const base=browserReady?`/api/projects/${r.project_id}/repair/${r.token}/files/`:null;
 $('qaComparisons').innerHTML=state.project.slides.map(s=>{
  const name=previews[s.number-1],hasPreview=!stale&&!!name,comparison=report?.visual_comparisons?.find(c=>c.slide_id===s.id),score=comparison?.objective_score?.score;
  const hasBrowser=browserReady&&r.diagnosis.pages.some(p=>p.slide_id===s.id),pending=hasBrowser&&!r.committed&&r.actions.length>0;
  const title=hasBrowser?(pending?'候选浏览器预览 · 尚未提交当前项目':r.committed?'已提交布局的浏览器验证截图':'当前项目浏览器检查截图'):'当前项目尚无对应浏览器验收截图';
  const status=hasBrowser?(pending?'候选与当前项目分开保存；提交后再导出，才包含这些修复':r.committed?'布局已提交；导出文件的哈希与兼容性另外检查':'没有修改对象；查看报告中的待复核证据'):'请生成修复候选并执行浏览器验收；下方旧导出仅作兼容性参考';
  const running=state.optimizationRun?.running,paused=state.optimizationRun?.disabled?.[s.id];
  const control=running?`<button class="btn small" data-page-optimization="${esc(s.id)}">${paused?'恢复此页优化':'暂停此页优化'}</button>`:'';
  const target=s.images.design?`<img src="${murl(s.images.design)}" alt="${esc(s.id)} 原始设计稿">`:'<div class="qa-preview-empty">尚未上传设计稿</div>';
  const primary=hasBrowser?`<img data-browser-candidate="${esc(s.id)}" src="${base}after/validation/${s.id}_stage.png" alt="${esc(s.id)} ${esc(title)}">`:'<div class="qa-preview-empty">尚无浏览器验收截图。生成候选后点击「浏览器验收候选」。</div>';
  const current=hasBrowser&&!r.committed?`<div><label>当前项目浏览器截图 · 修复前版本 ${r.base_revision}</label><img src="${base}before/validation/${s.id}_stage.png" alt="${s.id} 未提交修复的当前项目"></div>`:'';
  const auxiliary=hasPreview?`<img src="/build/${pid()}/${name}" alt="${s.id} LibreOffice辅助预览">`:`<div class="qa-preview-empty">${stale?'导出文件已过期，请重新导出':'没有辅助渲染预览'}</div>`;
  return `<article class="qa-page"><div class="qa-page-title"><strong>${esc(s.id)} · ${esc(s.title)}</strong><span>${esc(status)}</span>${control}</div><div class="qa-page-images"><div><label>目标设计稿</label>${target}</div><div><label>${esc(title)}</label>${primary}</div></div><details><summary>当前项目与导出文件的辅助证据（不会替代候选浏览器验收）</summary><button class="btn small" data-export-current>导出当前项目（不含候选）</button><div class="qa-page-images">${current}<div><label>${esc(report?.preview_renderer||'LibreOffice')} · 导出版本 ${report?.project_revision??'—'}${Number.isFinite(score)?` · 辅助指标 ${score}/100`:''}，不是候选验收结果</label>${auxiliary}</div></div></details></article>`;
 }).join('');
 $('qaComparisons').querySelectorAll('[data-export-current]').forEach(button=>button.onclick=()=>run(()=>exportDeck('editable',false,0,true)));
 $('qaComparisons').querySelectorAll('[data-page-optimization]').forEach(button=>button.onclick=()=>{const run=state.optimizationRun;if(!run?.running)return;const id=button.dataset.pageOptimization;run.disabled=run.disabled||{};run.disabled[id]=!run.disabled[id];logOptimization(id+' · '+(run.disabled[id]?'暂停':'恢复'),'仅控制此页后续样式建议；保留已有结果');renderQAImages()});
}
function renderStaleExportStatus(){const report=state.lastReport;if(!report||!state.project)return;const stale=report.project_revision!==state.project.revision||!report.artifact_sha256||report.artifact_valid_current===false;if(!stale)return;
 const reason=report.artifact_status||`报告版本 ${report.project_revision??'未知'}，当前项目版本 ${state.project.revision}`;
 $('renderReport').innerHTML=`<p class="warn">上次导出与当前项目不同步：${esc(reason)}。旧 PPTX 和旧预览已停用，请重新导出当前版本。</p><button class="btn primary" id="refreshCurrentExport">重新导出当前 PPTX</button>`;
 $('refreshCurrentExport').onclick=()=>run(async()=>{assignTab('qa');await exportDeck('editable',false)});
}
function assignTab(tab){if(tab==='lab'){run(()=>openManualDrawer());return;}state.tab=tab;try{if(tab==='qa'&&state.project)sessionStorage.setItem('visualPPTStudio.qaProject',state.project.id);else sessionStorage.removeItem('visualPPTStudio.qaProject')}catch{}window.StudioComponents.AppShell.setActiveNavigation(tab);if(tab==='upload')run(restoreAgentCandidatePreview);if(tab==='qa')run(restoreRepairCandidate);if(state.project)renderEverything();if(tab==='lab'&&state.project)run(labRefresh)}
async function saveSlide(){let s=slide();const changes={title:$('slideTitle').value,body:$('slideBody').value,intent:$('slideIntent').value,notes:$('slideNotes').value};
 await put(`/api/projects/${pid()}/slides/${s.id}`,changes);await loadKeep(s.id);toast('已保存 '+s.id+' 内容');}
async function loadKeep(sid){const id=pid();await loadProject(id,sid);}
async function getPrompts(){if(!state.project.locked)throw Error('请先锁定规划');return await api(`/api/projects/${pid()}/prompts`)}
async function lockPlan(){await post(`/api/projects/${pid()}/lock`,{locked:true});await loadKeep(state.sid);toast('已锁定页码，可使用S001…编号生成与上传图片')}
async function uploadOne(){const f=$('slotFile').files[0];if(!f)throw Error('请先选择图片');
 const d=new FormData();d.append('slide_id',$('slotSlide').value);d.append('role',$('slotRole').value);d.append('file',f);
 const sid=$('slotSlide').value,role=$('slotRole').value;
 const result=await api(`/api/projects/${pid()}/upload`,{method:'POST',body:d});$('slotFile').value='';if(state.uploadUndo?.slide_id===sid&&state.uploadUndo?.role===role){state.uploadUndo=null;localStorage.removeItem(uploadUndoKey(pid()))}await loadKeep(sid);toast(`上传完成：${result.filename}`+(result.aspect_warning?'（警告：图片非16:9）':''));
 if(role==='clean'&&slide().images.design&&slide().images.clean&&cleanPairCurrent(slide())){
  $('pairReview').textContent='图片已上传。DeepSeek 正在自动检查残字、构图和非文字图形…';
  try{await reviewImagePair(true,sid)}catch(e){$('pairReview').textContent='图片上传成功，但自动审核暂未完成：'+e.message+'。可稍后手动重试。'}
 }
}
async function removeCurrentUpload(){const s=slide(),role=$('slotRole').value;if(!s?.images?.[role])throw Error('当前页没有这类已上传图片');
 if(!confirm(`确定移除 ${s.id} 的「${roleLabel(role)}」？图片会暂存，可点击“撤回移除”恢复。`))return;
 const r=await post(`/api/projects/${pid()}/slides/${s.id}/images/${encodeURIComponent(role)}/remove`,{});state.uploadUndo={token:r.undo_token,slide_id:s.id,role,filename:r.removed};localStorage.setItem(uploadUndoKey(pid()),JSON.stringify(state.uploadUndo));await loadKeep(s.id);toast(`已移除 ${r.removed}，可在当前图片位置撤回`);
}
async function undoRemoveUpload(){const u=state.uploadUndo;if(!u)throw Error('没有可撤回的图片移除');
 await post(`/api/projects/${pid()}/uploads/${u.token}/restore`,{});state.uploadUndo=null;localStorage.removeItem(uploadUndoKey(pid()));await loadKeep(u.slide_id);toast(`已恢复 ${u.filename}`);
}
async function uploadMany(){const files=[...$('batchFiles').files];if(!files.length)throw Error('请选择多张图片');
 const d=new FormData();files.forEach(f=>d.append('files',f));const result=await api(`/api/projects/${pid()}/upload-batch`,{method:'POST',body:d});
 await loadKeep(state.sid);toast(`批量上传成功 ${result.uploaded.length} 张，按页码完成关联`);$('batchHint').textContent=result.uploaded.map(x=>x.filename).join('，');
 const candidateIds=[...new Set(result.uploaded.map(x=>x.slide_id))],ready=candidateIds.filter(id=>{const s=state.project.slides.find(x=>x.id===id);return s&&s.images.design&&s.images.clean&&cleanPairCurrent(s)});
 const box=$('batchPairReview');if(!ready.length){box.textContent='本批次没有新增/更新完整且有效配对的 design + clean；其他上传素材已保存。';return}
 const rows=[];box.textContent=`DeepSeek 正在逐页检查 ${ready.length} 组图片…`;
 for(const id of ready){try{const r=await reviewImagePair(true,id);rows.push(`${id}：${r.score}/100，${r.usable_as_clean?'可作 clean 候选':'需人工检查或重新生成'}${r.residual_text?.length?`，疑似残字 ${r.residual_text.length} 处`:''}`)}catch(e){rows.push(`${id}：审核失败，${e.message}`)}}
 box.textContent='批量图片配对审核结果\n'+rows.join('\n');}
async function applyElement(){const e=selected();if(!e)throw Error('请先选中元素');
 const previousBox=[...e.box],previousStyle={...(e.style||{})};
 e.text=$('elementText').value;e.box=['X','Y','W','H'].map(n=>Number($('el'+n).value));
 if(e.box.some(v=>!Number.isFinite(v)))throw Error('坐标请输入有效数字');
 e.style=e.style||{};e.style.size=Number($('elSize').value)||18;e.style.color=$('elColor').value.replace('#','');e.style.bold=$('elBold').checked;e.style.italic=$('elItalic').checked;e.style.font=$('elFont').value.trim();e.style.char_spacing_pt=Math.max(-5,Math.min(20,Number($('elCharSpacing').value)||0));e.style.line_spacing=Math.max(.7,Math.min(2.5,Number($('elLineSpacing').value)||1.05));e.style.vertical_align=$('elVerticalAlign').value;
 for(const [key,id] of Object.entries({margin_left_pt:'elMarginLeft',margin_right_pt:'elMarginRight',margin_top_pt:'elMarginTop',margin_bottom_pt:'elMarginBottom',paragraph_space_before_pt:'elParagraphBefore',paragraph_space_after_pt:'elParagraphAfter'}))e.style[key]=Math.max(0,Math.min(72,Number($(id).value)||0));
 for(const [key,value] of Object.entries(e.style||{}))if(value!==previousStyle[key])e.layout_locks={...(e.layout_locks||{}),[key]:true};
 if(e.box.some((v,i)=>Math.abs(v-previousBox[i])>1e-8))e.layout_locks={...(e.layout_locks||{}),x:true,y:true,w:true,h:true};
 if(e.kind==='image')e.asset=$('elementAsset').value;
 if(['rect','ellipse'].includes(e.kind))e.style.fill=e.style.color;
 if(e.kind==='text'||e.kind==='formula')await layoutCurrentElements();else renderEditor();toast('对象属性已修改，记得保存布局');}
function addElement(kind){const s=slide();const e={id:'e'+Date.now().toString(36),kind,box:[.16,.18,.32,.14],text:kind==='text'?'新文本':kind==='formula'?'\\frac{a}{b}':'',style:{size:kind==='formula'?24:22,color:state.project.design.ink,fill:state.project.design.accent,align:kind==='formula'?'center':'left'}};
 if(kind==='line')e.box=[.2,.35,.38,.008];
 if(kind==='formula')e.box=[.25,.42,.5,.16];
 if(kind==='image'){const role=Object.keys(s.images).find(k=>k.startsWith('asset_'));if(!role)throw Error('请先在「编号上传」添加S001_asset_01.png');e.asset=s.images[role];e.box=s.image_metadata?.[role]?.box||[.53,.28,.30,.40]}
 state.elements.push(e);state.selected=e.id;renderEditor();}
async function saveElements(){if(state.projectLoading||state.elsSid!==state.sid)throw Error('页面正在加载或元素尚未就绪，已阻止保存');const s=slide();const logicalCopy=field=>{const members=state.elements.filter(e=>e.logical_id===field||e.id===field);if(!members.length)return s[field];const lines=new Map();for(const e of members){const key=e.line_id||field;if(!lines.has(key))lines.set(key,[]);lines.get(key).push(e)}return [...lines.values()].map(line=>line.sort((a,b)=>(a.run_order||0)-(b.run_order||0)).map(e=>e.text||'').join('')).join('\n')};const title=logicalCopy('title'),body=logicalCopy('body');await put(`/api/projects/${pid()}/slides/${s.id}`,{elements:state.elements,title,body});await loadKeep(s.id);toast('已保存 '+s.id+' 的统一PPT布局；导出将使用这些坐标与字号');}
async function importWPSRender(){
 const report=state.lastReport;if(!report?.artifact_sha256||report.project_revision!==state.project.revision)throw Error('先导出当前项目 PPTX，再导入该文件的 WPS 结果');
 const file=$('wpsRenderFile').files?.[0];if(!file)throw Error('选择 WPS 导出的 PDF 或截图');
 const projectId=pid(),revision=state.project.revision;
 const data=new FormData();data.append('file',file);data.append('artifact_sha256',report.artifact_sha256);data.append('slide_id',$('wpsRenderSlide').value);data.append('wps_version',$('wpsRenderVersion').value);
 const crop=$('wpsRenderCrop').value.trim();if(crop)data.append('crop_json',JSON.stringify(crop.split(',').map(Number)));
 $('wpsRenderReport').textContent='正在导入 WPS 文件并对照原稿…';
 const result=await api(`/api/projects/${projectId}/wps-render`,{method:'POST',body:data});
 if(pid()!==projectId||state.project.revision!==revision)throw Error('项目已切换或修改，导入证据已归档但不能验证当前项目');
 $('wpsRenderReport').textContent=`${result.renderer} · ${result.wps_version} · 项目版本 ${result.project_revision} · 待人工验收（导入不代表视觉/可编辑性通过）\n`+pretty(result);
 $('wpsComparisons').innerHTML=result.pages.map(page=>{const source=state.project.slides.find(s=>s.id===page.slide_id);return `<article class="qa-page"><h3>${esc(page.slide_id)} · WPS 用户导出</h3><div class="qa-image-pair"><img src="/media/${projectId}/${source.images.design}" alt="原始设计稿"><img src="/wps-preview/${projectId}/${result.import_id}/${page.preview}" alt="WPS 实际导出"></div></article>`}).join('');
}
async function runQA(){let q=await api(`/api/projects/${pid()}/qa`);$('qaList').textContent=`硬错误 ${q.counts.errors} · 警告 ${q.counts.warnings}\n`+q.pages.map(s=>`${s.slide_id}: ${s.errors.map(x=>'❌ '+x).concat(s.warnings.map(x=>'⚠ '+x)).join('\n    ')||'✅ 规则检查通过'}`).join('\n');toast(q.ok?'几何/资源检查完成':'发现硬错误，请修复',!q.ok);return q}
async function exportDeck(mode='editable',supervise=false,round=0,exportCurrent=false){if(!exportCurrent&&mode==='editable'&&repairState?.project_id===pid()&&repairState.status==='validated_pending_commit'&&repairState.base_revision===state.project.revision)throw Error('修复候选尚未提交。请先点击“提交已验证修复”，再导出；候选预览不代表当前文件。需要原版本时，可在辅助证据中明确选择“导出当前项目”。');if(round===0&&supervise&&$('autoSupervise')?.checked&&$('autoApplyCalibration')?.checked){state.optimizationRun={best:{},stopped:{},disabled:{},pageStatus:{},noImprove:{},feedback:{},proposalAttempts:{},rejectedCandidates:{},lastCandidates:{},rollbackPending:{},stalledDiffs:{},rounds:0,log:[],memory:null,running:true,cancelRequested:false};$('aiCalibrate').textContent='停止自动优化';$('aiQAResult').textContent='自动优化已启动：保留上一轮可见预览；正在读取项目级经验记忆…';renderOptimizationLog();try{state.optimizationRun.memory=await api(`/api/projects/${pid()}/calibration-memory`)}catch(e){state.optimizationRun.memory={history:[],playbook:[]};logOptimization('经验记忆暂不可用',e.message,'error')}renderOptimizationLog()}else if(supervise&&round>0){$('aiQAResult').textContent=`第${round}轮候选已应用：上一轮成功渲染图仍保留在右侧；正在重新生成并实测…`;renderQAImages()}$('renderReport').textContent='正在调用真实PPTX导出和渲染程序…';let report=await post(`/api/projects/${pid()}/export?mode=${mode}`);state.lastReport=report;
 if(supervise&&state.optimizationRun?.cancelRequested){const run=state.optimizationRun;const restore=(report.visual_comparisons||[]).filter(c=>{const best=run.best?.[c.slide_id],score=c.objective_score?.score;return best&&Number.isFinite(score)&&(score<best.score||(score===best.score&&visualDifferenceProgress(best.differences||[],c.objective_score?.differences||[])<=0.001))});for(const c of restore){const s=state.project.slides.find(x=>x.id===c.slide_id),best=run.best[c.slide_id];if(s&&best?.elements)await put(`/api/projects/${pid()}/slides/${s.id}`,{elements:JSON.parse(JSON.stringify(best.elements)),title:s.title,body:s.body})}if(restore.length){await loadKeep(state.sid);report=await post(`/api/projects/${pid()}/export?mode=${mode}`);state.lastReport=report;renderQAImages()}run.running=false;$('aiCalibrate').textContent='启动样式诊断';$('aiQAResult').textContent=restore.length?'已停止优化；未提升的候选已自动恢复最佳版，并完成真实渲染。':'已停止优化；当前渲染不低于最佳版，已保留。';logOptimization('用户停止优化',$('aiQAResult').textContent);return report}
 const unsplit=(report.coverage||[]).filter(c=>c.ocr_text_regions>=5&&c.native_text_objects<Math.max(3,c.ocr_text_regions*.55));
 const comparisons=(report.visual_comparisons||[]).map(c=>c.error?`<li>${esc(c.slide_id)}：差异图生成失败：${esc(c.error)}</li>`:`<li>${esc(c.slide_id)}：自动实测 ${c.objective_score?.score??'不可评分'}/100（辅助分项指标）· 文字 ${c.objective_score?.components?.text??'—'} · 坐标 ${c.objective_score?.components?.position??'—'} · 格式 ${c.objective_score?.components?.typography??'—'} · 笔画粗细 ${c.objective_score?.components?.weight??'—'} · 中文 ${c.objective_score?.cjk_expected_regions?`${c.objective_score.cjk_detected_regions}/${c.objective_score.cjk_expected_regions}`:'—'} · 公式 ${c.objective_score?.formula_verified===false?'未通过':c.objective_score?.components?.formula??'—'} · 背景 ${c.objective_score?.components?.background??'—'} · 溢出风险 ${c.objective_score?.layout_warnings?.length||0} · 差异 ${(c.objective_score?.differences||[]).slice(0,8).map(d=>esc(describeVisualDifference(d))).join("；")||"未检测到显著文本差异"} · <a href="/lab/${pid()}/${esc(c.slide_id)}/${esc(c.diff)}" target="_blank" rel="noopener">查看差异热图</a></li>`).join('');
 const splitWarning=unsplit.length?unsplit.map(c=>`<p class="warn">${esc(c.slide_id)}：OCR检测到${c.ocr_text_regions}个文字区域，但只有${c.native_text_objects}个可编辑文字对象。当前版面还没拆分；DeepSeek不能把一个大文本框精确校成多框，因此本轮先不做无效校准。请使用第三步定向检查与修复。</p>`).join(''):'';
 $('renderReport').innerHTML=`<p class="good">已成功导出 ${esc(report.filename)}</p><p>${report.native_text?.ok===true?'✅ 原生文本逐项匹配':'文本校验：'+esc(report.native_text?.notice||'请检查')}</p><p>渲染预览：${report.previews.length}页${report.preview_renderer?` · 引擎：${esc(report.preview_renderer)}`:''}。预览和下载文件对应同一份PPTX。</p>${comparisons?`<p>逐页实测（非模型自评）</p><ul>${comparisons}</ul>`:''}${splitWarning}<p class="warn">${report.qa.warnings.some(w=>w.message.includes('栅格文字'))?'注意：本页仍有部分位图文字，仅被提升的区域可编辑。':'文字内容、公式内容及局部视觉仍需按实测分确认。'}</p><p>${esc(report.render_warning||'')}</p><a href="/build/${pid()}/${report.filename}" class="btn" download>下载 PPTX</a> ${report.pdf?`<a href="/build/${pid()}/${report.pdf}" class="btn" download>下载PDF</a>`:''}`;
 renderQAImages();toast('已导出PPTX（'+mode+'）');
 if(mode==='editable'&&supervise&&$('autoSupervise')?.checked&&$('autoApplyCalibration')?.checked){
  const measured=(report.visual_comparisons||[]).filter(c=>Number.isFinite(c.objective_score?.score));
  const measurement=measured.map(c=>`${c.slide_id} ${c.objective_score.score}/100（文字${c.objective_score.components?.text??'—'} · 位置${c.objective_score.components?.position??'—'} · 格式${c.objective_score.components?.typography??'—'} · 公式${c.objective_score.components?.formula??'—'} · 溢出风险${c.objective_score.layout_warnings?.length||0}）`).join('；');
   logOptimization(round===0?'优化前基线 · 实际渲染评分':`第${round}轮优化后 · 实际渲染评分`,measurement||'本轮没有可用的实测分数',measured.length?'info':'error');
  const unscored=(report.visual_comparisons||[]).filter(c=>c.error||!Number.isFinite(c.objective_score?.score));
  if(unscored.length){const detail=unscored.map(c=>`${c.slide_id}: ${c.error||c.objective_score?.notice||'实测评分不可用'}`).join('；');if(state.optimizationRun){state.optimizationRun.running=false;$('aiCalibrate').textContent='启动样式诊断'}$('aiQAResult').textContent=`自动优化因实测评分技术阻塞而暂停：${detail}。此页未达标，不会误标通过。`;logOptimization('评分技术阻塞',detail,'error');return report}
  const missingCjk=(report.visual_comparisons||[]).filter(c=>c.objective_score?.status==='cjk_text_not_detected');
  if(missingCjk.length){const reasons=missingCjk.map(c=>`${c.slide_id} 中文识别 ${c.objective_score.cjk_detected_regions}/${c.objective_score.cjk_expected_regions}`).join('；');$('aiQAResult').textContent=`自动优化暂停：${reasons}。当前中文字体/渲染不可验证，已阻止 DeepSeek 根据缺字画面乱调位置和字号。请确认本机字体后重新导出；系统只允许从检测到的常见字体列表中选择中文字体。`;logOptimization('中文渲染门禁',reasons+'；先修复真实渲染，再运行模型校准','error');if(state.optimizationRun){state.optimizationRun.running=false;$('aiCalibrate').textContent='启动样式诊断'}return report}
  if(unsplit.length){
   await stageRepairCheck();if(state.optimizationRun)state.optimizationRun.running=false;
   $('aiCalibrate').textContent='启动样式诊断';$('aiQAResult').textContent='原生文字覆盖不足，已停止 AI 参数校准。请使用上方定向修复候选；不会重跑第二步或覆盖已确认文字。';return report;
  }
  try{
   const run=state.optimizationRun||(state.optimizationRun={best:{},stopped:{},noImprove:{},feedback:{},proposalAttempts:{},rollbackPending:{},rounds:0,log:[],memory:{history:[],playbook:[]},running:true,cancelRequested:false});
   if(run.cancelRequested){run.running=false;$('aiCalibrate').textContent='启动样式诊断';$('aiQAResult').textContent='已按你的要求停止自动优化；保留当前最佳实测版本。';logOptimization('用户停止优化',$('aiQAResult').textContent);return report}
   const eligible=[];let rollback=false;
   for(const c of report.visual_comparisons||[]){const score=c.objective_score?.score;if(!Number.isFinite(score)||run.stopped[c.slide_id]||run.disabled?.[c.slide_id])continue;
    let best=run.best[c.slide_id];const priorBestScore=best?.score??null,validatingRollback=!!run.rollbackPending?.[c.slide_id];let acceptedDifferenceProgress=false;
    const localRegressions=best&&!validatingRollback?visualDifferenceRegressions(best.differences||[],c.objective_score?.differences||[]):[];
    if(validatingRollback){delete run.rollbackPending[c.slide_id];logOptimization(`${c.slide_id} · 已恢复最佳版`,`回滚后实测 ${score}/100；这不是新的优化候选，将基于上轮反馈重新请求`)}
    else if(!best){run.best[c.slide_id]={score,differences:c.objective_score?.differences||[],elements:JSON.parse(JSON.stringify(state.project.slides.find(s=>s.id===c.slide_id)?.elements||[]))};best=run.best[c.slide_id]}
    else if(round>0&&!localRegressions.length&&(score>best.score||(score===best.score&&visualDifferenceProgress(best.differences||[],c.objective_score?.differences||[])>0.001))){const sameScore=score===best.score;acceptedDifferenceProgress=sameScore;run.best[c.slide_id]={score,differences:c.objective_score?.differences||[],elements:JSON.parse(JSON.stringify(state.project.slides.find(s=>s.id===c.slide_id)?.elements||[]))};best=run.best[c.slide_id];run.noImprove[c.slide_id]=0;delete run.feedback[c.slide_id];run.rejectedCandidates[c.slide_id]=[];if(sameScore)logOptimization(`${c.slide_id} · 全页差异改善`,`总分仍为 ${score}/100（取整后不变），本轮全页实测差异净改善；已保留候选并继续优化。`)}
    else if(run.noCandidatePending?.[c.slide_id]){
     // DeepSeek returned no executable parameter after its bounded retries.
     // The deck was not changed, so do not perform a pointless rollback or
     // count an unchanged render as another failed visual candidate.
     delete run.noCandidatePending[c.slide_id];run.noCandidateStreak=run.noCandidateStreak||{};run.noCandidateStreak[c.slide_id]=(run.noCandidateStreak[c.slide_id]||0)+1;
     if(run.noCandidateStreak[c.slide_id]>=2){run.stopped[c.slide_id]='连续两轮没有可执行参数，已停止空转并保留当前最佳实测版';logOptimization(`${c.slide_id} · 无变化保护停止`,`${run.stopped[c.slide_id]}。`,'error')}
     else logOptimization(`${c.slide_id} · 跳过空候选回滚`,`本轮没有任何可执行参数，当前实测版本未被修改；再给一轮整页尝试，如果仍无可执行变化就自动停止。`);
    }
    else if(round>0&&(score<=best.score||localRegressions.length)&&!validatingRollback){const tries=(run.noImprove[c.slide_id]||0)+1;run.noImprove[c.slide_id]=tries;if(tries>=2)run.stopped[c.slide_id]='连续两轮无对象级改善，停止AI样式诊断并保留最佳版';
     const detail=(state.calibrationDeck||[]).find(x=>x.slide_id===c.slide_id)?.result;
     const rejected=run.lastCandidates?.[c.slide_id]||[];rememberRejectedCalibration(run,c.slide_id,rejected)
     const regressionNote=localRegressions.length?`即使整页分数变化为 ${score-best.score>=0?'+':''}${score-best.score}，逐对象实测发现显著回归：${localRegressions.map(x=>`${x.id||x.type} ${x.type} +${x.increase.toFixed(3)}`).join('；')}。`:'';
     run.feedback[c.slide_id]=`上轮真实渲染第${round}轮得分${score}/100，当前最佳${best.score}/100，未提升或有局部显著回归（连续${tries}次）。${regressionNote}模型问题诊断：${JSON.stringify(detail?.issues||[])}；系统校验风险：${JSON.stringify(detail?.layout_issues||[])}。上轮全页实测差异：${JSON.stringify(run.roundDiffsBefore?.[c.slide_id]||[])}。请继续整体检查本页所有可修复差异，不要因某个对象上轮无改善而放弃该对象；改用不同且更有针对性的参数，并联合修复相互遮挡/挤压的多个文字框。严禁重复已被实渲染否决的参数。`;
     logOptimization(`${c.slide_id} · 候选未保留`,`本轮 ${score}/100，最佳 ${best.score}/100；${regressionNote||'整页没有提升'}已回滚最佳版。连续未提升 ${tries} 次；下一轮继续整页复核并更换联合修复策略。`);
     const s=state.project.slides.find(x=>x.id===c.slide_id);if(s){await put(`/api/projects/${pid()}/slides/${s.id}`,{elements:JSON.parse(JSON.stringify(best.elements)),title:s.title,body:s.body});run.rollbackPending[c.slide_id]=true;rollback=true}
    }
    const learningEntry=(state.calibrationDeck||[]).find(x=>x.slide_id===c.slide_id&&x.ok),learning=learningEntry?.result;
    const differencesBefore=run.roundDiffsBefore?.[c.slide_id]||[];
    const differencesAfter=c.objective_score?.differences||[];
    if(round>0){const beforeKinds=differencesBefore.map(d=>d.type),afterKinds=differencesAfter.map(d=>d.type);logOptimization(`${c.slide_id} · 第${round}轮变化核对`,`真实评分 ${priorBestScore??'—'}→${score}（本轮变化 ${priorBestScore===null?'—':`${score-priorBestScore>=0?'+':''}${score-priorBestScore}`}）；差异数 ${differencesBefore.length}→${differencesAfter.length}；仍未解决：${differencesAfter.slice(0,10).map(describeVisualDifference).join('；')||'无'}；上轮候选参数：${JSON.stringify(learning?.corrections||[]).slice(0,2200)}；结果：${score>(priorBestScore??-1)&&!localRegressions.length?'实测提升，保留':'未提升或有局部回归，恢复最佳版'}`)}
    const outcome=validatingRollback?'rollback':round===0?'baseline':(priorBestScore!==null&&!localRegressions.length&&(score>priorBestScore||acceptedDifferenceProgress)?'improved':'no_improvement');
    const properties=[...new Set((learning?.corrections||[]).flatMap(x=>[...(x.box?['box']:[]),...Object.keys(x.style||{})]))];
    try{const saved=await post(`/api/projects/${pid()}/calibration-memory`,{slide_id:c.slide_id,round:round+1,before:priorBestScore,after:score,outcome,properties,issues:learning?.issues||[],corrections:learning?.corrections||[],differences_before:differencesBefore,differences_after:differencesAfter,candidate_applied:!!learningEntry?.autoApplied,prompt_insight:outcome==='improved'?learning?.prompt_insight||'':''});
     const mem=run.memory||(run.memory={history:[],playbook:[]});mem.history=mem.history||[];mem.playbook=mem.playbook||[];const delta=priorBestScore===null?null:score-priorBestScore;const historyRow={slide_id:c.slide_id,round:round+1,before:priorBestScore,after:score,delta,outcome,properties,issues:learning?.issues||[],corrections:learning?.corrections||[],differences_before:differencesBefore,differences_after:differencesAfter,candidate_applied:!!learningEntry?.autoApplied};mem.history.push(historyRow);mem.history=mem.history.slice(-80);if(outcome==='improved'&&learning?.prompt_insight)mem.playbook.push({slide_id:c.slide_id,round:round+1,delta,properties,guidance:learning.prompt_insight,validated_by_render:true});mem.playbook=mem.playbook.slice(-20);mem.playbook_version=saved.playbook_version;renderOptimizationLog()
    }catch(e){logOptimization(`${c.slide_id} · 记忆写入失败`,e.message,'error')}
    const remainingActionable=differencesAfter.filter(d=>deepSeekCalibratableTypes.has(d.type));
    if(learningEntry?.autoApplied&&run.noCandidateStreak)run.noCandidateStreak[c.slide_id]=0;
    if(!shouldContinueCalibration(score,differencesAfter)){run.stopped[c.slide_id]='无剩余可执行样式差异';run.noImprove[c.slide_id]=0;logOptimization(`${c.slide_id} · 达标`,`${score}/100，且没有剩余可由现有文字对象修复的实测差异，停止继续修改`)}
    else if(!run.stopped[c.slide_id]){eligible.push(c.slide_id);if(score>=90)logOptimization(`${c.slide_id} · 局部差异继续校准`,`${score}/100 已达分数线，但仍有 ${remainingActionable.length} 项实测差异，将继续整页联合校准`)}
   }
   if(rollback){await loadKeep(state.sid);if(!eligible.length){run.running=false;$('aiCalibrate').textContent='启动样式诊断';renderQAImages();$('aiQAResult').textContent=`自动校准已结束：${Object.entries(run.stopped).map(([id,msg])=>`${id} ${msg}`).join('；')||'所有未达标页均已单独暂停'}。最佳版已恢复并保留。`;logOptimization('优化结束',$('aiQAResult').textContent);return report}return exportDeck(mode,true,round+1)}
   if(!eligible.length){run.running=false;$('aiCalibrate').textContent='启动样式诊断';renderQAImages();const paused=Object.keys(run.disabled||{}).filter(id=>run.disabled[id]);$('aiQAResult').textContent=`自动校准已结束：${Object.entries(run.stopped).map(([id,msg])=>`${id} ${msg}`).join('；')||'所有未达标页均已单独暂停'}${paused.length?`；已暂停 ${paused.join('、')}`:''}。`;logOptimization('优化结束',$('aiQAResult').textContent);return report}
   if(round>=3){run.running=false;for(const id of eligible)run.stopped[id]=`达到3轮样式诊断预算，最佳实测${run.best[id]?.score??'未知'}/100，仍有待复核差异`;$('aiCalibrate').textContent='启动样式诊断';$('aiQAResult').textContent=`自动优化达到3轮样式诊断预算：${eligible.map(id=>`${id} 最佳${run.best[id]?.score??'未知'}/100，保留逐对象检查结果`).join('；')}。保留最佳版本，可检查逐项差异或重新启动。`;logOptimization('达到最大轮次',$('aiQAResult').textContent,'error');return report}
   run.rounds=round+1;$('aiQAResult').textContent=`第${round+1}轮实测完成；DeepSeek 正在为 ${eligible.join('、')} 分析可编辑文字框，并提出格式/位置候选…`;
   logOptimization(`第${round+1}轮 · DeepSeek校准`,`${eligible.join('、')}：整体检查全页差异，可联合修复多个关联文字对象；模型自评分仅作诊断`);
   const adjusted=await aiCalibrateDeck(eligible);if(state.optimizationRun?.cancelRequested){const active=state.optimizationRun;active.running=false;$('aiCalibrate').textContent='启动样式诊断';$('aiQAResult').textContent='已停止优化；没有再启动下一轮。';logOptimization('用户停止优化',$('aiQAResult').textContent);return state.lastReport}if(state.optimizationRun?.fatalError){renderQAImages();return report}if(adjusted){logOptimization(`第${round+1}轮 · 候选已应用`,'候选已写入PPT对象，下一步马上真实渲染；只有实测提升才保留');return exportDeck(mode,true,round+1)}
   logOptimization(`第${round+1}轮 · 无可执行候选`,'无有效样式候选，停止调用并保留当前版本');run.running=false;$('aiCalibrate').textContent='启动样式诊断';return report
  }
  catch(e){if(state.optimizationRun){state.optimizationRun.running=false;$('aiCalibrate').textContent='启动样式诊断'}$('aiQAResult').textContent='PPTX 已导出，但自动逐页监督遇到技术阻塞并暂停：'+e.message+'。仍有待复核差异的页面不会标记完成；修复阻塞后可继续。'}
 }
 return report;}
function pretty(o){return json(o)}
function differenceKey(d){
 const ids=[d.id,...(d.objects||[])].filter(x=>typeof x==='string'&&x).join(',');
 const semantic=d.expected_text||d.text||d.expected||d.metric||(d.expected_box||[]).join(',')||'';
 // The same property may be wrong on many objects on one slide. Include the
 // measured object identity so progress/stall tracking never collapses (for
 // example) every glyph_size or color error into a single map entry.
 return `${d.type}|${ids||semantic}|${semantic}|${d.metric||''}`;
}
function differencePriority(d,elements=[]){
 const base=({missing_text:100,text_content:96,formula_mismatch:94,text_overlap:92,glyph_size:88,text_weight:84,text_color:78,position_or_size:74})[d.type]||50;
 const byId=new Map((elements||[]).map(e=>[e.id,e]));
 const boxes=[d.expected_box,byId.get(d.id)?.box,...(d.objects||[]).map(id=>byId.get(id)?.box)].filter(b=>Array.isArray(b)&&b.length>=4&&Number.isFinite(Number(b[2]))&&Number.isFinite(Number(b[3])));
 const area=boxes.length?Math.min(.04,boxes.reduce((sum,b)=>sum+Math.max(0,Number(b[2]))*Math.max(0,Number(b[3])),0)):0.0015;
 // Prefer defects that occupy more slide area: repeatedly enlarging a tiny
 // badge digit should not outrank a visibly displaced heading or crowded card.
 const areaWeight=Math.max(.45,Math.min(1.75,Math.sqrt(Math.max(.0002,area)/.0015)));
 const severity=Math.max(.75,Math.min(1.35,.75+differenceDistance(d)*.35));
 return base*areaWeight*severity;
}
const deepSeekCalibratableTypes=new Set(['glyph_size','text_weight','text_color']);
function isDeepSeekBillingFailure(error){return /HTTP\s*402|insufficient\s+balance|insufficient_balance|余额不足/i.test(String(error?.message||error||''))}
function shouldContinueCalibration(score,differences){return (differences||[]).some(d=>deepSeekCalibratableTypes.has(d.type))}
function validatePageWideCalibrationCandidate(result,scopeDiffs=[],elements=[]){
 const allowedStyle=new Set(['size','font','eastAsianFont','color','bold','italic']);
 let stripped=0;
 for(const c of result.corrections||[]){
  if(c.box){delete c.box;stripped++}
  const style=c.style||{};
  for(const key of Object.keys(style))if(!allowedStyle.has(key)){delete style[key];stripped++}
  c.style=style;
 }
 // Keep joint box proposals for every related object. A simple edge-to-edge
 // gap check wrongly rejects useful moves while two boxes still overlap, even
 // when the rendered overlap is reduced. Real PPTX rendering and scoring are
 // the acceptance gate for geometry; only unsupported style keys are stripped.
 if(stripped)result.issues=[...(result.issues||[]),`系统仅拦截了${stripped}项属性白名单外参数；多个关联对象的框位候选会一起进入真实PPTX渲染验证。`];
 return stripped;
}
function enforceGlyphSizeRequirements(result,requirements=[],elements=[]){
 const byId=new Map((elements||[]).map(e=>[e.id,e])),corrections=new Map((result.corrections||[]).map(c=>[c.id,c]));let adjusted=0;
 for(const req of requirements||[]){
  const current=byId.get(req.id);if(!current||!Array.isArray(current.box)||current.box.length!==4)continue;
  const c=corrections.get(req.id)||{id:req.id,target_text:req.target_text||current.text||'',box:[...current.box],style:{...(current.style||{})},reason:'系统按原稿与实渲染字形差异补齐联动字号、文本框高度和中心位置'};
  const oldBox=Array.isArray(c.box)&&c.box.length===4?c.box:current.box,box=oldBox.map(Number),currentBox=current.box.map(Number);
  const size=Math.max(Number(c.style?.size)||0,Number(current.style?.size)||0,Number(req.min_size)||0);
  const width=Math.max(0,Number(box[2])||Number(currentBox[2])||0),height=Math.max(Number(box[3])||0,Number(currentBox[3])||0,Number(req.min_box_height)||0);
  const delta=Array.isArray(req.box_center_delta)?req.box_center_delta:[0,0];
  const centerX=currentBox[0]+currentBox[2]/2+(Number(delta[0])||0),centerY=currentBox[1]+currentBox[3]/2+(Number(delta[1])||0);
  const next=[centerX-width/2,centerY-height/2,width,height];
  next[0]=Math.min(Math.max(0,next[0]),Math.max(0,1-width));next[1]=Math.min(Math.max(0,next[1]),Math.max(0,1-height));
  c.box=next;c.style={...(c.style||{}),size};if(!c.target_text)c.target_text=req.target_text||current.text||'';
  if(!corrections.has(req.id)){(result.corrections||(result.corrections=[])).push(c);corrections.set(req.id,c)}
  if(JSON.stringify(oldBox)!==JSON.stringify(next)||Number(c.style.size)!==Number(current.style?.size))adjusted++;
 }
 if(adjusted)result.issues=[...(result.issues||[]),`系统依据原稿/实渲染字形实测，联动校正了${adjusted}个对象的字号、框高和框中心；候选仍会经过真实PPT渲染验收。`];
 return adjusted;
}
function differenceObjectIds(diffs=[]){return [...new Set((diffs||[]).flatMap(d=>[d.id,...(d.objects||[])].filter(x=>typeof x==='string'&&x)))];}
function differenceDistance(d){switch(d.type){case'missing_text':return 10;case'text_content':case'formula_mismatch':return 8*(1-(Number.isFinite(d.similarity)?d.similarity:0));case'text_color':{const hex=v=>{const s=String(v||'').replace('#','');return /^[0-9a-f]{6}$/i.test(s)?[0,2,4].map(i=>parseInt(s.slice(i,i+2),16)):null},a=hex(d.expected),b=hex(d.actual);return a&&b?Math.sqrt(a.reduce((n,v,i)=>n+(v-b[i])**2,0))/441.7:1}case'position_or_size':return Math.max(0,Number(d.center_delta)||0)*2+(1-(Number(d.width_score)||0))+(1-(Number(d.height_score)||0));case'glyph_size':return Math.abs((Number(d.expected_height)||0)-(Number(d.actual_height)||0))*10;case'text_weight':return Math.abs(Math.log(Math.max(.05,Number(d.ratio)||1)));case'text_overlap':return Math.max(0,(Number(d.actual_overlap)||0)-(Number(d.expected_overlap)||0))*4;default:return 1}}
function visualDifferenceProgress(before=[],after=[]){const prior=new Map(before.map(d=>[differenceKey(d),d]));const next=new Map(after.map(d=>[differenceKey(d),d]));let gain=0;for(const [key,d] of prior){const remaining=next.get(key);gain+=differenceDistance(d)-(remaining?differenceDistance(remaining):0)}for(const [key,d] of next)if(!prior.has(key))gain-=differenceDistance(d);return gain}
function visualDifferenceRegressions(before=[],after=[],threshold=.16){const prior=new Map(before.map(d=>[differenceKey(d),d]));return after.filter(d=>['text_overlap','position_or_size','glyph_size'].includes(d.type)).map(d=>({id:d.id,type:d.type,increase:differenceDistance(d)-(prior.has(differenceKey(d))?differenceDistance(prior.get(differenceKey(d))):0)})).filter(x=>x.increase>threshold)}
function describeVisualDifference(d){const labels={missing_text:'缺失文字',text_content:'文字识别不一致',position_or_size:'位置/尺寸偏差',text_color:'文字颜色偏差',glyph_size:'实际字高偏差',text_weight:'字重/笔画粗细偏差',text_overlap:'文字区域互相遮挡',formula_mismatch:'公式内容/运算符不一致'};const label=labels[d.type]||d.type;const parts=[];if(d.expected!==undefined)parts.push(`原稿：${d.expected}`);if(d.actual!==undefined)parts.push(`实际：${d.actual}`);if(d.expected_box&&d.actual_box)parts.push(`OCR字形框 原稿 ${d.expected_box.join(',')} → 实际 ${d.actual_box.join(',')}`);if(d.editable_box)parts.push(`对应PPT文本框 ${d.editable_box.join(',')}`);if(d.center_delta!==undefined)parts.push(`中心偏移 ${d.center_delta}`);if(d.expected_height!==undefined)parts.push(`字高 ${d.expected_height} → ${d.actual_height}`);if(d.expected_stroke_ratio!==undefined)parts.push(`笔画厚度/字高 原稿 ${d.expected_stroke_ratio} → 实际 ${d.actual_stroke_ratio}（${d.direction==='actual_lighter'?'实际偏细':'实际偏粗'}）`);else if(d.expected_density!==undefined)parts.push(`墨迹覆盖 原稿 ${d.expected_density} → 实际 ${d.actual_density}（仅作辅助）`);if(d.expected&&d.actual&&d.type==='text_color')parts.push(`颜色 #${d.expected} → #${d.actual}`);if(d.similarity!==undefined)parts.push(`匹配度 ${d.similarity}`);if(d.actual_overlap!==undefined)parts.push(`实渲染重叠 ${(d.actual_overlap*100).toFixed(0)}%（原稿 ${Math.round((Number(d.expected_overlap)||0)*100)}%）`);if(d.text)parts.push(d.text);return `${label}${parts.length?'：'+parts.join('；'):''}`}
async function debugSection(part){const d=part==='health'?await api('/api/health'):part==='projects'?await api(`/api/projects/${pid()}/debug`):part==='qa'?await api(`/api/projects/${pid()}/qa`):part==='plan'?slide():state.project;
 $('debugOutput').textContent=pretty(d)}
function renderProposal(target,body){let e=$(target);e.classList.remove('hidden');e.innerHTML=body;}
async function aiPlan(){const result=await post(`/api/projects/${pid()}/plan/ai`,aiReq());state.proposal=result.proposal;
 renderProposal('aiPlanProposal','<h3>DeepSeek逐页内容建议（不自动覆盖）</h3>'+result.proposal.map((p,i)=>`<div><b>S${String(i+1).padStart(3,'0')} ${esc(p.title)}</b><p>${esc(p.body).replaceAll('\n','<br>')}</p></div>`).join('')+'<button class="btn primary" id="applyAIPlan">应用全部建议到规划</button>');
 $('applyAIPlan').onclick=()=>run(async()=>{for(let i=0;i<state.proposal.length;i++){let p=state.proposal[i],s=state.project.slides[i];await put(`/api/projects/${pid()}/slides/${s.id}`,p)}await loadKeep(state.sid);toast('已应用AI建议，请检查后锁定')});}
async function aiStyle(){const r=await post(`/api/projects/${pid()}/style/ai`,aiReq());renderProposal('aiStyleProposal',`<h3>AI视觉风格建议（不自动应用）</h3><pre class="output">${esc(pretty(r.proposal))}</pre><button class="btn primary" id="applyStyle">应用调色板</button>`);
 $('applyStyle').onclick=()=>run(async()=>{await put(`/api/projects/${pid()}/design`,{...r.proposal.palette,font:r.proposal.font});await loadKeep(state.sid);toast('已应用视觉调色板')});}
async function aiImagePrompts(){const s=slide();const r=await post(`/api/projects/${pid()}/slides/${s.id}/prompts/ai`,aiReq());state.aiImagePrompts[s.id]=r;renderPrompts();toast('双阶段 Image Prompt 已生成；分别复制完整设计稿和无字图编辑提示词。');}
function cleanPairCurrent(s){const m=s?.image_metadata?.clean;return !!(s?.images?.design&&s?.images?.clean&&m&&!m.stale_source&&m.source_design_sha256===s.image_metadata?.design?.sha256)}
function boxOverlap(a,b){const x=Math.max(0,Math.min(a[0]+a[2],b[0]+b[2])-Math.max(a[0],b[0])),y=Math.max(0,Math.min(a[1]+a[3],b[1]+b[3])-Math.max(a[1],b[1]));const inter=x*y,union=a[2]*a[3]+b[2]*b[3]-inter;return union>0?inter/union:0}
function normalizedText(s){return String(s||'').toLowerCase().replace(/[\s\p{P}\p{S}]/gu,'')}
function ocrLineGroup(sourceText,sourceBox){const b=(sourceBox||[]).map(v=>Number(v).toFixed(4)).join(',');return `ocr-row:${b}:${normalizedText(sourceText).slice(0,96)}`}
function regionContainment(a,b){const x=Math.max(0,Math.min(a[0]+a[2],b[0]+b[2])-Math.max(a[0],b[0])),y=Math.max(0,Math.min(a[1]+a[3],b[1]+b[3])-Math.max(a[1],b[1]));return a[2]*a[3]>0?x*y/(a[2]*a[3]):0}
function fontStackForRegion(textClass,latinFont,eastAsianFont){
 const primary=textClass==='cjk'?(eastAsianFont||latinFont||'Noto Sans SC'):(latinFont||'Noto Sans');
 return `"${String(primary).replace(/["\\]/g,'')}", sans-serif`;
}
async function reflowOCRTextBoxes(regions,sid=state.sid){
 const projectId=pid();
 const response=await post(`/api/projects/${projectId}/slides/${sid}/text-layout`,{regions});
 if(state.project?.id!==projectId||state.sid!==sid)throw Error('页面已切换，布局候选未写入其他页面');
 await ensureProjectFonts({id:projectId,slides:[{elements:response.regions}]});return response.regions;
}
async function layoutCurrentElements(){
 const sid=state.sid,projectId=pid();
 const response=await post(`/api/projects/${projectId}/slides/${sid}/text-layout`,{elements:state.elements});
 if(state.project?.id!==projectId||state.sid!==sid)throw Error('页面已切换，布局候选未写入其他页面');
 state.elements=response.elements;state.elsSid=sid;await ensureProjectFonts({id:projectId,slides:[{elements:response.elements}]});renderEditor();
 toast(`已生成统一布局候选，${response.layout.warnings.length} 个对象有提示；检查后保存布局`);
 return response;
}
function hasCandidateConflict(r){return !!r.ocr_conflicts?.length||(r.text_layout?.layout_warning||[]).some(w=>w.startsWith('layout_conflict:'))}
function candidateStatusLabel(r){
 if((r.text_layout?.layout_warning||[]).some(w=>w.startsWith('layout_conflict:')))return '布局异常 · 点击调整';
 if(r.ocr_conflicts?.length)return '识别冲突 · 点击核对';
 if(r.duplicate_of_existing)return '重复候选 · 未选中';
 if(r.role==='ignore')return '已忽略';
 return '未选中';
}
function agentCandidateMarkup(regions){
 return regions.filter(r=>String(r.text||'').trim()&&((r.editable&&r.role!=='ignore')||hasCandidateConflict(r))).map(r=>{
  const [x,y,w,h]=r.box,st=r.style||{},estimate=Math.round(h*720*(r.role==='title'?.78:.70));
  const size=Math.max(6,Number(st.size)||Math.max(9,Math.min(54,estimate)));
  const latin=browserFontFamily(r,st.font||state.project.design.font),east=browserFontFamily(r,st.eastAsianFont||'Noto Sans SC'),fontStack=fontStackForRegion(r.text_class,latin,east);
  return `<div class="auto-preview-text ${r.editable&&r.role!=='ignore'?'':'pending-candidate'}" data-review-region="${esc(r.id)}" role="button" tabindex="0" title="${esc(r.editable?'点击调整文字':candidateStatusLabel(r))}" style="left:${x*100}%;top:${y*100}%;width:${w*100}%;height:${h*100}%;font-size:${size/9.6}cqw;color:${csshex(st.color||state.project.design.ink)};font-family:${esc(fontStack)};font-weight:${st.bold?'700':'400'}">${esc(String(r.text||''))}${r.editable&&r.role!=='ignore'?'':`<span class="candidate-status">${esc(candidateStatusLabel(r))}</span>`}</div>`
 }).join('');
}
function bindAgentCandidatePreview(out){
 out.querySelectorAll('[data-review-region]').forEach(node=>{
  const open=()=>run(async()=>{await openManualDrawer();focusManualRegion(state.labRegions.findIndex(r=>r.id===node.dataset.reviewRegion))});
  node.onclick=open;node.onkeydown=event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();open()}};
 });
}
async function restoreAgentCandidatePreview(){
 if(!state.project||state.autoCandidateSid||$('autoRebuildPage').disabled)return;
 const id=pid(),sid=state.sid,out=$('autoRebuildResult');if(out.querySelector('.auto-preview-stage'))return;
 const result=await api(`/api/projects/${id}/slides/${sid}/reconstruction`);
 if(id!==state.project?.id||sid!==state.sid||state.autoCandidateSid||$('autoRebuildPage').disabled)return;
 if(!result.ocr?.source_valid||!result.ocr.regions?.length)return;
 state.labRegions=result.ocr.regions;labRenderRegions();
 out.innerHTML=`<p>已恢复本页识别候选：包括未选中及越界文字。异常候选仍显示；悬停查看状态，点击文字打开右侧编辑。</p><div class="auto-preview-stage"><img alt="候选文字预览" src="${murl(slide().images.clean||slide().images.design)}">${agentCandidateMarkup(state.labRegions)}</div>`;
 bindAgentCandidatePreview(out);
}
async function autoRebuildSlide(){
 const button=$('autoRebuildPage');if(button.disabled)return;button.disabled=true;
 try{return await autoRebuildSlideWork()}
 catch(error){
  state.autoCandidateSid=null;state.autoCandidateKind=null;$('confirmAutoRebuild').disabled=true;
  run(()=>openManualDrawer(error.message||String(error)));
  const out=$('autoRebuildResult');out.textContent=`本页 Agent 未完成：${error.message||String(error)}。已保存的页面未被替换；请检查待复核区域后重试。`;
  throw error;
 }finally{button.disabled=false}
}
async function autoRebuildSlideWork(){
 const sid=state.sid,s=slide();if(!s?.images.design)throw Error('先上传本页完整设计稿');if(!cleanPairCurrent(s))throw Error('先为本页上传与当前 design 配对的 clean 无字底图');
 const out=$('autoRebuildResult'),confirmButton=$('confirmAutoRebuild');confirmButton.disabled=true;state.autoCandidateSid=null;state.autoCandidateKind=null;
 out.textContent='Agent 1/4：DeepSeek 检查 design 与 clean 配对…';
 let pair;try{pair=await reviewImagePair(true,sid)}catch(e){out.textContent=`DeepSeek 配对检查未完成：${e.message}。图片和项目没有被修改；可重试，或进入高级 OCR 流程手动接管。`;throw e}if(sid!==state.sid)throw Error('当前页面已切换，请重新运行本页 Agent');
 // A current hash-verified design/clean pair is sufficient for editable text
 // reconstruction. Vision may call intentionally removed text (titles, labels)
 // “damage”; its aesthetic verdict must not block OCR or rewrite the clean.
 const verifiedCleanFallback=!pair.usable_as_clean&&!pair.repairable_clean&&cleanPairCurrent(s)&&pair.source_pair_current!==false;
 if(!pair.usable_as_clean&&!pair.repairable_clean&&!verifiedCleanFallback){out.textContent=`配对检查 ${pair.score}/100：clean 与当前 design 的来源校验未通过，不能安全叠加文字。请重新上传配对底图后重试。`;return}
 out.textContent='Agent 2/4：本地 OCR 与 DeepSeek 结构分析并行处理中…';
 const [ocrResult,visionResult]=await Promise.allSettled([
  post(`/api/projects/${pid()}/slides/${sid}/ocr?backend=auto`),
  post(`/api/projects/${pid()}/slides/${sid}/analyze-structure`,aiReq())
 ]);
 if(sid!==state.sid)throw Error('当前页面已切换，已停止写入；请在目标页重新运行 Agent');
 if(ocrResult.status!=='fulfilled')throw ocrResult.reason;
 const ocr=ocrResult.value,vision=visionResult.status==='fulfilled'?visionResult.value:null;
 const structure=vision?.objects?.filter(o=>o.kind==='text')||[];
 const lineRegions=ocr.regions||[];
 let regions=lineRegions.map((r,i)=>{
  const txt=normalizedText(r.text),plannedTitle=normalizedText(s.title);
  const best=r.structure?{o:r.structure,overlap:1}:structure.map(o=>({o,overlap:regionContainment(r.box,o.box)})).sort((a,b)=>b.overlap-a.overlap)[0];
  // Every OCR baseline remains its own editable text box. Structure proposals
  // can supply styling, but never merge multiple visual lines into one object.
  let role='custom';if(txt&&plannedTitle&&txt.length>=5&&(plannedTitle.includes(txt)||txt.includes(plannedTitle)))role='title';
  else if(best?.overlap>=.50&&best.o.confidence>=.72&&['title','ignore'].includes(best.o.role))role=best.o.role;
  const confidence=Math.max(0,Math.min(1,Number(r.confidence)||0));
  // Small labels and slide numbers are systematically scored lower because
  // their glyphs occupy few pixels. Keep a lower threshold for compact OCR
  // boxes while leaving the regular confidence gate unchanged for body text.
  const compactText=(Number(r.box?.[3])||0)<=.026&&String(r.text||'').trim().length<=14;
  // A vision model may mistake a page number or tiny caption for decoration.
  // Strong OCR evidence for compact text takes precedence over that semantic
  // label, and its pixel-derived size/color stay authoritative.
  if(compactText&&confidence>=.78&&role==='ignore')role='custom';
  const editable=!r.review_required&&role!=='ignore'&&(confidence>=.86||(compactText&&confidence>=.78));
  const visionStyle=!compactText&&best?.overlap>=.50&&best.o.confidence>=.72?best.o.style||{}:{};
  return {...r,id:`text_${String(i+1).padStart(3,'0')}`,role,confidence,editable,
   // OCR glyph-height measurement is a better first-pass font-size signal than
   // the vision model's coarse semantic estimate (which tended to assign 18pt
   // to tiny captions and stretched lines across adjacent cards).
   style:{...(r.style||{}),...(visionStyle.color&&!r.style?.color?{color:visionStyle.color.replace(/^#/, '').toUpperCase()}:{}),...(typeof visionStyle.bold==='boolean'?{bold:visionStyle.bold}:{})}};
 });
 const keptExistingText=(s.elements||[]).filter(e=>!String(e.id).startsWith('ocr_')&&!['title','body'].includes(e.id)&&e.kind==='text'&&String(e.text||'').trim());
 for(const r of regions){const duplicate=keptExistingText.some(e=>normalizedText(e.text)===normalizedText(r.text)&&regionContainment(r.box,e.box)>=.45);if(duplicate){r.role='ignore';r.editable=false;r.duplicate_of_existing=true}}

 const cleanup=[];
 if(pair.repairable_clean){
  for(const [i,residual] of pair.residual_text.entries()){
   const best=regions.map(r=>({r,overlap:boxOverlap(r.source_box||r.box,residual.box)})).sort((a,b)=>b.overlap-a.overlap)[0];
   let region=best?.overlap>=.12?best.r:null;
   if(!region){region={id:`residual_${String(i+1).padStart(3,'0')}`,text:residual.text||'',box:[...residual.box],role:'custom',confidence:residual.confidence||0,editable:false};regions.push(region)}
   cleanup.push({text:region.source_text||region.text||residual.text||'',box:[...(region.source_box||region.box)],confidence:region.confidence||residual.confidence||0});
   if(region.confidence>=.88&&!region.review_required)region.editable=true;
  }
 }
 regions=await reflowOCRTextBoxes(regions,sid);
 for(const r of regions){
  const conflicts=(r.text_layout?.layout_warning||[]).filter(x=>x.startsWith('layout_conflict:'));
  if(conflicts.length){r.editable=false;r.review_required=true;r.layout_review_reason=`布局冲突（${conflicts.join('；')}），请核对字体、字号和页面边界`}
 }
 state.labRegions=regions;labRenderRegions();await put(`/api/projects/${pid()}/slides/${sid}/regions`,{regions});
 let selected=regions.filter(r=>r.editable&&r.role!=='ignore');
 for(const r of regions)r.review_reason=r.layout_review_reason?r.layout_review_reason:r.review_required?`同一区域OCR冲突：${(r.ocr_conflicts||[]).map(x=>x.text).join(' / ')}，请核对原稿`:r.duplicate_of_existing?'与现有对象重复':r.role==='ignore'?'语义标记为忽略':!r.editable?'置信度不足，待人工确认':null;
 if(!selected.length&&!cleanup.length){state.autoCandidateSid=null;out.textContent=`OCR 已识别 ${regions.length} 个区域，但没有达到自动提升置信度阈值的文字。为避免错字被写入，请在右侧「手动调整」校正并勾选区域后再继续。${vision?` DeepSeek 识别了 ${structure.length} 个文字对象。`: ' DeepSeek 结构分析暂不可用。'}`;assignTab('lab');return}
 let previewUrl=murl(s.images.clean),repairReport=null;
 if(cleanup.length){
  out.textContent='Agent 3/4：clean 有少量残字，正在基于已上传 clean 局部修复（不会从 design 重绘整张）…';
  const candidate=await post(`/api/projects/${pid()}/slides/${sid}/clean-candidate`,{regions:cleanup,mask_mode:'ink',method:'telea',padding:4,threshold:32,source_role:'clean'});
  if(sid!==state.sid)throw Error('当前页面已切换，候选未提交到其他页面');
  state.autoCandidateKind='repair';previewUrl=labUrl(candidate.candidate);repairReport=candidate.report;
 }else state.autoCandidateKind='existing_clean';
 state.autoCandidateSid=sid;
 const warning=verifiedCleanFallback?'AI对底图的视觉判定未通过；但文件来源哈希匹配，保留clean不改动并继续按OCR拆分文字':vision?`DeepSeek 结构分析 ${structure.length} 个对象候选`:'DeepSeek 结构分析暂不可用，使用 OCR 结果继续';
 const low=regions.length-selected.length;
 const previewText=agentCandidateMarkup(regions);
 out.innerHTML=`<p><b>Agent 处理完成 · ${sid}</b></p><p>配对 ${pair.score}/100 · OCR识别 ${ocr.regions?.length||0} 个区域 → 按换行及中英/数字/公式类型拆为 ${regions.length} 个独立文本框 · 统一后端测宽与基线 · 自动提升 ${selected.length} 个 · 待人工复核 ${low} 个 · ${esc(warning)}</p>${repairReport?`<p>clean 局部修复候选：${repairReport.method} · 遮罩 ${repairReport.mask_percent}% 。`:'<p>沿用已上传且通过配对检查的 clean；没有从 design 重新生成或擦除整页。</p>'}<div class="auto-preview-stage"><img alt="配对无字底图与待提升文字的叠加预览" src="${previewUrl}">${previewText}</div>${regions.some(r=>!r.editable)?`<details><summary>待复核文字清单（${regions.filter(r=>!r.editable).length}）</summary>${regions.filter(r=>!r.editable).map(r=>`<p>${esc(r.id)} · ${esc(r.text)} · ${esc(r.review_reason||'待确认')}</p>`).join('')}</details>`:''}<p class="hint">每个视觉行单独成框；一行中的中文、英文/数字和公式也分别成框，按阅读顺序相邻摆放。同级列表行会对齐首字并统一样式。字色优先取原稿与配对无字底图的像素差；对比度不足时先用黑/白中更清晰的颜色，DeepSeek 后续可按原稿微调。检查叠加预览后确认；确认仅保存文字对象。</p>`;
 bindAgentCandidatePreview(out);
 confirmButton.disabled=false;confirmButton.textContent=`确认保存本页 · ${selected.length} 框`;
 if(vision?.objects?.length)state.sceneProposal=vision;
}
async function confirmAutoRebuild(automatic=false){
 const sid=state.sid;if(state.autoCandidateSid!==sid)throw Error('本页还没有新的 Agent 候选，请先运行自动处理');
 let regions=await reflowOCRTextBoxes(labReadRows(),sid);state.labRegions=regions;labRenderRegions();
 const promoted=regions.filter(r=>r.editable&&r.role!=='ignore');const conflicts=promoted.filter(r=>(r.text_layout?.layout_warning||[]).some(w=>w.startsWith('layout_conflict:')));if(conflicts.length){await openManualDrawer(`请先调整 ${conflicts.length} 个布局冲突：${conflicts.map(r=>r.text.slice(0,30)).join('；')}`);throw Error('部分文字框仍有布局冲突，请在右侧手动调整后保存')}if(!promoted.length&&state.autoCandidateKind!=='repair')throw Error('没有勾选可编辑文字框');
 await put(`/api/projects/${pid()}/slides/${sid}/regions`,{regions});
 if(!automatic&&!confirm(`确认保存 ${sid} 的 ${promoted.length} 个原生文字框？保存后继续留在第二步，不影响其他页面。`))return;
 const clean=state.autoCandidateKind==='repair'?await post(`/api/projects/${pid()}/slides/${sid}/accept-clean`):null;
 const s=slide(),els=JSON.parse(JSON.stringify(s.elements||[])).filter(e=>!String(e.id).startsWith('ocr_')&&!['title','body'].includes(e.id));
 for(const r of promoted){
  if(!String(r.text||'').trim())continue;
  els.push({...r,id:`ocr_${String(r.run_id||r.id).replace(/[^a-zA-Z0-9_.:-]/g,'').slice(-70)}`,kind:r.kind||'text',text:String(r.text),box:[...r.box],style:{...(r.style||{})}});
 }
 await put(`/api/projects/${pid()}/slides/${sid}`,{elements:els,title:s.title,body:s.body});
 await loadKeep(sid);await post(`/api/projects/${pid()}/slides/${sid}/confirm-reconstruction`);await refreshReconstructionProgress();state.autoCandidateSid=null;state.autoCandidateKind=null;
 const savedSummary=`已确认并保存 ${sid}。${clean?`clean 局部候选已通过确认：${clean.filename}；`: '沿用原配对 clean；'}按换行和中英/公式类型拆分并提升 ${promoted.length} 个独立可编辑文本框。`;
 const progress=state.reconstructionPages||[],done=progress.filter(x=>x.ready&&x.confirmed).length,total=progress.length;
 const out=$('autoRebuildResult'),button=$('confirmAutoRebuild');button.disabled=true;button.textContent='本页第二步已保存';
 out.textContent=`${savedSummary}第二步进度 ${done}/${total} 页。当前仍停留在第二步，请选择其他页面继续运行 Agent；全部页面完成后，再点击下方统一进入第三步。`;
 renderAgentReadiness();
}
async function finishReconstructionAndEnterThirdStage(){
 if(state.secondStageTransitioning)throw Error('整套 PPTX 正在导出处理中');state.secondStageTransitioning=true;renderReconstructionProgress();
 try{await refreshReconstructionProgress();const pages=state.reconstructionPages||[],blocked=pages.filter(x=>!x.ready||!x.confirmed);
  if(blocked.length){const detail=blocked.map(x=>`${x.slide_id}：${!x.ready?'缺素材或配对无效':'尚未确认保存'}`).join('；');throw Error(`还有页面未完成第二步：${detail}`)}
  const savedSummary=`全套 ${pages.length} 页均已分别完成第二步并保存。现在统一导出整套 PPTX${$('autoSupervise')?.checked&&$('autoApplyCalibration')?.checked?' 并启动整页 DeepSeek 校准':'并进入第三步'}。`;
  await continueRebuildToThirdStage(state.sid,savedSummary);
 }finally{state.secondStageTransitioning=false;renderReconstructionProgress()}
}
async function continueRebuildToThirdStage(sid,savedSummary){
 const button=$('finishReconstruction');button.disabled=true;button.textContent='正在导出整套并进入第三步…';
 $('autoRebuildResult').textContent=savedSummary+'正在导出整套真实 PPTX，并由 DeepSeek 按整页实测结果校准。';
 toast('第二步已保存，正在进入第三步真实渲染和 DeepSeek 校准');assignTab('qa');
 try{
  if($('autoSupervise')?.checked&&$('autoApplyCalibration')?.checked){
   await exportDeck('editable',true);
   $('autoRebuildResult').textContent=savedSummary+`第三步已运行。${$('aiQAResult').textContent||'最新 PPTX 已真实渲染并启动整页 DeepSeek 校准。'} 可在「真实 PPTX 输出报告」和逐页预览中查看结果。`;
  }else{
   await exportDeck('editable',false);
   $('autoRebuildResult').textContent=savedSummary+'第三步已完成真实 PPTX 渲染；自动校准开关处于关闭状态。若要让 DeepSeek 精细调整，请开启“导出后视觉检查”和“自动应用候选”，再启动样式诊断。';
  }
  button.textContent='整套已进入第三步';state.autoStageRetry=null;
 }catch(e){
  button.disabled=false;button.textContent='重试第三步校准';
  state.autoStageRetry=()=>finishReconstructionAndEnterThirdStage();
  $('autoRebuildResult').textContent=savedSummary+`第三步启动失败：${e.message}。第二步已保存，可点击此按钮重试校准。`;
  throw e;
 }
}
async function reviewImagePair(quiet=false,sid=state.sid){const s=state.project.slides.find(x=>x.id===sid);if(!s||!cleanPairCurrent(s))throw Error(`${sid} 没有与当前设计稿有效配对的 clean 底图`);if(!quiet)$('pairReview').textContent='DeepSeek 正在对照完整设计稿和无字底图…';const r=await post(`/api/projects/${pid()}/slides/${sid}/review-image-pair`,aiReq());
 state.pairReports[sid]=r;
 if(sid!==state.sid)return r;
 const rows=[...(r.residual_text||[]).map(x=>`残留文字：${x.text||'未识别文字'} · ${(x.confidence*100).toFixed(0)}% · [${x.box.map(v=>Number(v).toFixed(3)).join(', ')}]`),...(r.layout_changes||[]).map(x=>'构图变化：'+x),...(r.nontext_damage||[]).map(x=>'非文字损伤：'+x),...(r.recommendations||[]).map(x=>'建议：'+x)];
 $('pairReview').textContent=`DeepSeek 配对评分 ${r.score}/100 · ${r.usable_as_clean?'建议可作 clean 候选':'建议先重新生成/人工检查'} · 哈希配对${r.source_pair_current?'有效':'无效'}\n${rows.join('\n')||'没有发现明显残留文字或结构变化。'}\n${r.notice}`;
 $('refineCleanPrompt').disabled=!r.source_pair_current;return r;}
function renderPairReport(){const r=state.pairReports[state.sid];$('refineCleanPrompt').disabled=!r?.source_pair_current;
 if(r){const rows=[...(r.residual_text||[]).map(x=>`残留文字：${x.text||'未识别文字'} · ${(x.confidence*100).toFixed(0)}%`),...(r.layout_changes||[]).map(x=>'构图变化：'+x),...(r.nontext_damage||[]).map(x=>'非文字损伤：'+x),...(r.recommendations||[]).map(x=>'建议：'+x)];$('pairReview').textContent=`DeepSeek 配对评分 ${r.score}/100 · ${r.usable_as_clean?'建议可作 clean 候选':'建议先重新生成/人工检查'} · 哈希配对${r.source_pair_current?'有效':'无效'}\n${rows.join('\n')||'没有发现明显残留文字或结构变化。'}\n${r.notice}`}
 else $('pairReview').textContent='上传配对后的 design 与 clean 时会自动审核，也可手动复查。';}
async function refineCleanPrompt(){const s=slide(),review=state.pairReports[s.id];if(!review)throw Error('先运行图片配对审核');
 const r=await post(`/api/projects/${pid()}/slides/${s.id}/prompts/refine-clean`,{...aiReq(),current_prompt:$('promptClean').value,review});
 state.aiImagePrompts[s.id]={...(state.aiImagePrompts[s.id]||{}),clean_prompt:r.clean_prompt};renderPrompts();
 const rows=[...(r.focus_regions||[]).map(x=>`<li>${esc(x)}</li>`),...(r.checks||[]).map(x=>`<li>验收：${esc(x)}</li>`)];
 renderProposal('refinedPromptResult',`<h3>下一轮 Image 无字图编辑提示词</h3><p>${esc(r.notice)}</p>${rows.length?`<ul>${rows.join('')}</ul>`:''}<textarea rows="8" readonly>${esc(r.clean_prompt)}</textarea><button class="btn" id="copyRefinedPrompt">复制修订提示词</button>`);
 $('copyRefinedPrompt').onclick=()=>run(async()=>{await navigator.clipboard.writeText(r.clean_prompt);toast('已复制修订后的 Image 编辑提示词')});toast('DeepSeek 已根据图片质检结果优化下一轮去字提示词');}
async function analyzeVisual(){const r=await post(`/api/projects/${pid()}/slides/${state.sid}/analyze`,aiReq());
 renderProposal('aiVisualProposal',`<h3>AI 布局识别建议</h3><p>${esc(r.notice)}</p><pre class="output">${esc(pretty(r.elements))}</pre><button class="btn primary" id="applyVisual">应用标题和正文框坐标</button>`);
 $('applyVisual').onclick=()=>{state.elements=state.elements.filter(e=>!['title','body'].includes(e.id)).concat(r.elements);state.selected='title';renderEditor();toast('已应用到预览，记得保存布局')};}
async function aiVisualQA(){const r=await post(`/api/projects/${pid()}/slides/${state.sid}/visual-qa`,aiReq());$('aiQAResult').textContent=pretty(r);}
let repairState=null,repairPoll=null,repairAutoPromise=null;
const REPAIR_AUTO_KEY='visualPPTStudio.repairAutoRun';
function repairScope(){return $('repairWholeDeck').checked?{}:{slides:[state.sid]}}
function setRepairAutoMarker(projectId,token){try{sessionStorage.setItem(REPAIR_AUTO_KEY,JSON.stringify({project_id:projectId,token}))}catch{}}
function clearRepairAutoMarker(projectId,token){try{const marker=JSON.parse(sessionStorage.getItem(REPAIR_AUTO_KEY)||'null');if(marker?.project_id===projectId&&(!token||marker.token===token))sessionStorage.removeItem(REPAIR_AUTO_KEY)}catch{}}
function repairAutoMarker(){try{return JSON.parse(sessionStorage.getItem(REPAIR_AUTO_KEY)||'null')}catch{return null}}
function renderRepairDiagnosis(d){
 $('repairOverview').textContent=`检查来源版本 ${d.revision} · ${d.issue_count} 项问题线索 · AI调用 ${d.ai_calls||0}。接下来会自动完成候选验收；没有通过验证的修复不会写入项目。`;
 $('repairIssues').innerHTML=d.pages.map(p=>`<details open class="repair-page"><summary>${esc(p.slide_id)} · ${p.issues.length} 项 · ${p.status==='structural_checks_pass'?'结构检查通过':'待处理'}</summary>${p.issues.map(i=>`<div class="repair-issue"><strong>${esc(i.issue_type)} · ${esc(i.text)}</strong>${i.line_id?`<p>行 ${esc(i.line_id)} · ${i.run_ids?.length||0} 个关联 run</p>`:''}<p>${esc(({create_native_text:'可定向补建原生文字',repair_line:'可生成整行几何修复候选',repair_source_style:'可生成原字体字重与定位候选'})[i.suggested_action]||'需要复核，不自动修改')} · 置信度 ${(i.confidence*100).toFixed(0)}%</p><details><summary>证据与匹配对象</summary><pre>${esc(json({source_region:i.source_region,matched_elements:i.matched_elements,evidence:i.evidence}))}</pre></details></div>`).join('')}</details>`).join('');
}
function renderRepairCandidate(r){
 repairState=r;$('repairCandidate').classList.remove('hidden');
 const base=`/api/projects/${r.project_id}/repair/${r.token}/files/`;
 const noAction=r.status==='candidate_created'&&!r.actions.length;
 const marker=repairAutoMarker(),legacyPending=r.status==='validated_pending_commit'&&!(marker?.project_id===r.project_id&&marker.token===r.token);
 const noActionHint=noAction?`<div class="notice warning"><b>本次没有生成修复动作。</b>已检查 ${r.diagnosis?.issue_count||0} 项，DeepSeek 调用 ${r.ai_calls||0} 次，AI提出 ${r.diagnosis?.ai_proposal_count||0} 项。未通过内容、Clean底图和对象位置证据门槛的问题保留为待复核；请展开下方诊断查看原因。</div>`:'';
 const candidateLabels={candidate_created:'已生成，尚未验收',validating:'浏览器验收中',validated_pending_commit:legacyPending?'上一轮候选已验收，尚未保存':'定向验收通过，正在自动保存',requires_review:'待人工复核',validation_failed:'验收失败',committed:'已提交',checked_current_project:'当前项目检查完成'};
 const legacyHint=legacyPending?'<div class="notice warning"><b>这是旧版手动流程留下的候选，尚未写入项目。</b>为避免误提交旧结果，自动流程不会擅自保存它；点击上方按钮会按当前项目重新检查。</div>':'';
 $('repairCandidate').innerHTML=`<strong>候选 ${esc(candidateLabels[r.status]||r.status)} · ${r.actions.length} 个定向动作 · DeepSeek ${r.ai_calls||0} 次</strong><p>${esc(r.phase||'候选已保存，尚未提交')}${r.error?' · '+esc(r.error):''}</p>${noActionHint}${legacyHint}<details><summary>诊断、AI建议、已执行动作与回滚记录</summary><pre>${esc(json({diagnosis:r.diagnosis,actions:r.actions.map(a=>({slide_id:a.slide_id,text:a.text,action:a.action,before:a.before_parameters,after:a.parameters})),rejected_actions:r.rejected_actions||[]}))}</pre></details><a href="${base}candidate.pptx" download>候选PPTX</a> · <a href="${base}original_project.json">原项目备份</a>${r.comparison&&r.status!=='validating'&&r.validation_complete!==false?`<p><a href="${base}diagnostic_summary.html" target="_blank">分类诊断及解决/回滚统计</a> · <a href="${base}before/validation/report.html" target="_blank">修复前浏览器报告</a> · <a href="${base}after/validation/report.html" target="_blank">修复后浏览器报告及逐对象对照</a> · <a href="${base}after/validation/export.pptx" download>浏览器验收PPTX</a></p>${(r.comparison.targeted||[]).map(t=>`<p><a href="${base}${esc(t.crop)}" target="_blank">原稿 / 修复前 / 修复后局部对照</a></p>`).join('')}<pre>${esc(json(r.comparison))}</pre>`:''}`;
 const autoButton=$('repairAutoRun');if(autoButton)autoButton.disabled=r.status==='validating'||!!repairAutoPromise;renderQAImages();
}
async function restoreRepairCandidate(){
 const id=pid();clearTimeout(repairPoll);repairState=null;$('repairCandidate').classList.add('hidden');if($('repairAutoRun'))$('repairAutoRun').disabled=!!repairAutoPromise;
 renderQAImages();const r=await api(`/api/projects/${id}/repair/latest`);if(id!==pid())return;if(r){renderRepairDiagnosis(r.diagnosis);renderRepairCandidate(r);const marker=repairAutoMarker();if(marker?.project_id===id&&marker.token===r.token&&r.committed)clearRepairAutoMarker(id,r.token);else if(marker?.project_id===id&&marker.token===r.token&&!r.committed&&['candidate_created','validating','validated_pending_commit'].includes(r.status)){repairAutoPromise=resumeRepairAutoRun(id,r.token);try{await repairAutoPromise}finally{repairAutoPromise=null;if($('repairAutoRun'))$('repairAutoRun').disabled=false}}}
}
async function stageRepairCheck(){const id=pid(),d=await post(`/api/projects/${id}/repair/check`,repairScope());if(id===pid())renderRepairDiagnosis(d);return d}
const REPAIR_TERMINAL=new Set(['validated_pending_commit','requires_review','validation_failed','committed','checked_current_project']);
const waitRepairTick=()=>new Promise(resolve=>setTimeout(resolve,1500));
async function pollRepairUntilTerminal(projectId,token){
 let transientErrors=0;
 for(;;){
  let r;try{r=await api(`/api/projects/${projectId}/repair/${token}`);transientErrors=0}catch(e){if(++transientErrors>=3)throw Error(`读取自动验收状态连续失败：${e.message}`);await waitRepairTick();continue}
  if(projectId===state.project?.id&&repairState?.token===token)renderRepairCandidate(r);
  if(REPAIR_TERMINAL.has(r.status)&&r.validation_complete!==false)return r;
  await waitRepairTick();
 }
}
async function validateRepairAndWait(projectId,token){
 const current=await api(`/api/projects/${projectId}/repair/${token}`);
 let r=current;
 if(current.status==='candidate_created'||current.status==='requires_review'&&!current.validation_complete){
  if(projectId===state.project?.id)$('repairOverview').textContent='候选已隔离保存；正在浏览器实渲染并检查文字、几何和 PPTX 结构…';
  r=await post(`/api/projects/${projectId}/repair/${token}/validate`);
 }
 if(projectId===state.project?.id)renderRepairCandidate(r);
 if(REPAIR_TERMINAL.has(r.status)&&r.validation_complete!==false)return r;
 return await pollRepairUntilTerminal(projectId,token);
}
async function finishRepairAutoRun(projectId,token){
 let r=await validateRepairAndWait(projectId,token);
 if(r.status==='validated_pending_commit'){
  if(projectId!==pid())return r
  $('repairOverview').textContent='浏览器局部对比和 PPTX 结构契约均通过；正在保存候选。系统会保留提交前备份。';
  r=await post(`/api/projects/${projectId}/repair/${token}/commit`);
  clearRepairAutoMarker(projectId,token);
  if(projectId===pid())await loadKeep(state.sid);
  if(projectId===state.project?.id)renderRepairCandidate(r);
  if(projectId===state.project?.id)$('repairOverview').textContent=`自动修复已保存（项目版本 ${r.committed_revision??'已更新'}）。提交前备份和逐对象验收报告均已保留；请重新导出以生成最新版 PPTX。`;
  if(projectId===pid())toast('已保存通过浏览器与 PPTX 验收的修复；原项目备份已保留');return r;
 }
 clearRepairAutoMarker(projectId,token);
 const summary={requires_review:'验收完成，但仍有问题待复核；原项目未修改。请查看下方报告中的未通过项和回滚原因。',validation_failed:'自动验收失败；候选已隔离，原项目未修改。可查看验收报告和错误原因。',checked_current_project:'检查完成，没有需要修复的确定性问题；原项目未修改。',candidate_created:'没有可自动验收的安全修复动作；原项目未修改。'};
 if(projectId===state.project?.id){$('repairOverview').textContent=summary[r.status]||`自动流程结束，状态：${r.status}。原项目未提交，请查看候选诊断。`;renderRepairCandidate(r)}
 if(projectId===pid())toast(r.status==='requires_review'?'验收完成：部分问题需人工复核，项目未修改':'自动检查完成：原项目未修改');return r;
}
async function resumeRepairAutoRun(projectId,token){try{return await finishRepairAutoRun(projectId,token)}catch(e){if(projectId===state.project?.id)$('repairOverview').textContent=`自动流程暂停：${e.message}。候选仍隔离保存，原项目未修改；可再次点击自动运行继续验收。`;throw e}}
async function stageRepairAutoRun(){
 if(repairAutoPromise)return repairAutoPromise;
 const id=pid();repairAutoPromise=(async()=>{
  const button=$('repairAutoRun');button.disabled=true;
  try{
   $('repairIssues').innerHTML='';$('repairCandidate').classList.add('hidden');
   const marker=repairAutoMarker();
   if(marker?.project_id===id&&marker.token){
    let pending=null;try{pending=await api(`/api/projects/${id}/repair/${marker.token}`)}catch{}
    if(pending?.committed||pending?.status==='committed'){
     clearRepairAutoMarker(id,marker.token);renderRepairCandidate(pending);
     $('repairOverview').textContent=`上次自动修复已保存（项目版本 ${pending.committed_revision??'已更新'}），本次不重复运行。`;
     return pending;
    }
    if(pending&&!pending.committed&&['candidate_created','validating','validated_pending_commit'].includes(pending.status)){
     repairState=pending;renderRepairDiagnosis(pending.diagnosis);renderRepairCandidate(pending);
     $('repairOverview').textContent='发现上次中断的候选；正在从已保存的验收状态继续，不会重复调用 DeepSeek…';
     return await finishRepairAutoRun(id,marker.token);
    }
    clearRepairAutoMarker(id,marker.token);
   }
   $('repairOverview').textContent='第 1/3 步：检查原文、OCR、Clean 底图和现有文字对象；必要时按页调用一次 DeepSeek 查漏…';
   const created=await post(`/api/projects/${id}/repair/smart-candidates`,{...aiReq(),...repairScope()});
   if(created.token&&created.actions?.length)setRepairAutoMarker(id,created.token);
   if(id!==pid())throw Error('当前项目已切换；候选仍保存在原项目中，没有自动提交。重新打开原项目后可继续。');
   renderRepairDiagnosis(created.diagnosis);renderRepairCandidate(created);
   if(!created.actions?.length){$('repairOverview').textContent=created.diagnosis?.issue_count?`检查完成：发现 ${created.diagnosis.issue_count} 项线索，但没有满足自动修复证据门槛的动作。原项目未修改；请查看待复核理由。`:'检查完成，没有发现需要自动修复的问题；原项目未修改。';return created}
   if(!created.token)throw Error('服务端返回了修复动作，但缺少候选编号；为保护原项目，未继续提交。');
   setRepairAutoMarker(id,created.token);
   $('repairOverview').textContent=`第 2/3 步：已为 ${created.actions.length} 个动作创建隔离候选，正在用浏览器实渲染和 PPTX 契约验证…`;
   return await finishRepairAutoRun(id,created.token);
  }catch(e){if(id===pid())$('repairOverview').textContent=`自动流程未完成：${e.message}。如已生成候选，它仍与原项目隔离；未通过服务端验收的候选不会写入。`;throw e
  }finally{repairAutoPromise=null;if(button.isConnected)button.disabled=false}
 })();
 return repairAutoPromise;
}

async function aiCalibrate(){const r=await post(`/api/projects/${pid()}/slides/${state.sid}/calibrate-text`,aiReq());showCalibration(r);return r;}
function showCalibration(r){state.calibration=r;
 const body=`<h3>DeepSeek 校准候选 · 模型诊断 ${r.score}/100（非验收分）</h3><p>${esc(r.notice)}</p><pre class="output">${esc(pretty({issues:r.issues,corrections:r.corrections}))}</pre><button class="btn primary" id="applyCalibration" ${r.corrections.length?'':'disabled'}>在编辑器预览应用这些坐标/样式</button>`;
 renderProposal('calibrationProposal',body);$('applyCalibration').onclick=()=>run(async()=>{for(const c of state.calibration.corrections){const e=state.elements.find(x=>x.id===c.id);if(!e||!['text','formula'].includes(e.kind))continue;if(c.box)e.box=[...c.box];e.style={...(e.style||{}),...c.style};}state.selected=state.calibration.corrections[0]?.id||state.selected;renderEditor();assignTab('editor');toast('校准建议已用于本地预览，请逐项检查并点击「保存布局」')});}
function rememberRejectedCalibration(run,sid,candidates){if(!run||!sid||!Array.isArray(candidates))return;const bucket=run.rejectedCandidates[sid]||(run.rejectedCandidates[sid]=[]);for(const candidate of candidates){const key=JSON.stringify({id:candidate.id,box:candidate.box,style:candidate.style||{}});if(!bucket.some(item=>JSON.stringify({id:item.id,box:item.box,style:item.style||{}})===key))bucket.push(candidate)}run.rejectedCandidates[sid]=bucket.slice(-8)}
function calibrationCandidateWasRejected(candidate,rejected){
 const styleKey=value=>JSON.stringify(Object.fromEntries(Object.entries(value||{}).sort(([a],[b])=>a.localeCompare(b))));
 return (rejected||[]).some(old=>{
  if(old?.id!==candidate?.id||styleKey(old?.style)!==styleKey(candidate?.style))return false;
  const oldHasBox=Array.isArray(old?.box)&&old.box.length===4,newHasBox=Array.isArray(candidate?.box)&&candidate.box.length===4;
  if(!oldHasBox&&!newHasBox)return true;
  if(oldHasBox!==newHasBox)return false;
  return old.box.every((value,index)=>Number.isFinite(value)&&Number.isFinite(candidate.box[index])&&Math.abs(value-candidate.box[index])<=.004);
 });
}
function compactCalibrationFeedback(previous,diffs,recent,learned,rejected,strategy,scopeDiffs=[],scopeIds=[],elements=[]){
 const compactDiffs=[];for(const item of [...(diffs||[])].sort((a,b)=>differencePriority(b)-differencePriority(a))){const d={type:item.type};for(const key of ['id','expected_text','reference_region_id'])if(item[key])d[key]=String(item[key]).slice(0,100);if(item.text)d.text=String(item.text).slice(0,72);if(Array.isArray(item.objects))d.objects=item.objects.slice(0,4);if(Array.isArray(item.object_texts))d.object_texts=item.object_texts.slice(0,4).map(x=>String(x||'').slice(0,80));for(const key of ['expected','actual','direction'])if(item[key]!==undefined)d[key]=typeof item[key]==='string'?item[key].slice(0,48):item[key];for(const key of ['expected_box','actual_box','editable_box'])if(Array.isArray(item[key]))d[key]=item[key].map(v=>Number.isFinite(v)?Math.round(v*1000)/1000:v);if(item.editable_boxes&&typeof item.editable_boxes==='object')d.editable_boxes=item.editable_boxes;if(item.editable_style&&typeof item.editable_style==='object')d.editable_style=item.editable_style;for(const key of ['center_delta','width_score','height_score','expected_height','actual_height','expected_density','actual_density','expected_stroke_ratio','actual_stroke_ratio','stroke_ratio','ratio','similarity','expected_overlap','actual_overlap'])if(Number.isFinite(item[key]))d[key]=Math.round(item[key]*1000)/1000;compactDiffs.push(d)}
 // Current measured differences are already sent in full above. Duplicating
 // both prior-round full difference arrays here bloats the request past the
 // server/model input limits without adding evidence; retain full prior
 // parameters and a compact type count while the persistent memory keeps the
 // complete raw history for review.
 const differenceCounts=rows=>(rows||[]).reduce((counts,d)=>{counts[d.type]=(counts[d.type]||0)+1;return counts},{});
 const compactHistoryDiff=d=>{const row={type:d.type};for(const key of ['id','expected_text','text','expected','actual','direction','expected_height','actual_height','expected_overlap','actual_overlap','ratio'])if(d[key]!==undefined)row[key]=typeof d[key]==='string'?d[key].slice(0,100):d[key];for(const key of ['expected_box','actual_box','editable_box','objects'])if(Array.isArray(d[key]))row[key]=d[key].slice(0,4);return row};
 const summarizeHistoryRound=x=>({slide_id:x.slide_id,round:x.round,outcome:x.outcome,before:x.before,after:x.after,delta:x.delta,properties:x.properties||[],issues:x.issues||[],previous_parameters:(x.corrections||[]).map(c=>{const related=d=>d.id===c.id||(d.objects||[]).includes(c.id);return {id:c.id,target_text:c.target_text,box:c.box,style:c.style||{},reason:c.reason||'',candidate_applied:!!x.candidate_applied,rendered_effect:{before:(x.differences_before||[]).filter(related).map(compactHistoryDiff),after:(x.differences_after||[]).filter(related).map(compactHistoryDiff)}}}),previous_difference_counts:{before:differenceCounts(x.differences_before),after:differenceCounts(x.differences_after)},candidate_applied:x.candidate_applied});
 const latestRound=(recent||[]).slice(-1)[0],lastModifiedRound=[...(recent||[])].reverse().find(x=>(x.corrections||[]).length>0);
 const history=latestRound?[summarizeHistoryRound(latestRound)]:[],last_modified_round=lastModifiedRound?summarizeHistoryRound(lastModifiedRound):null;
 const playbook=(learned||[]).slice(-2).map(x=>({properties:(x.properties||[]).slice(0,5),guidance:String(x.guidance||'').slice(0,160)}));
 const rejectedCompact=(rejected||[]).slice(-4).map(x=>({id:x.id,target_text:x.target_text,box:x.box,style:Object.fromEntries(Object.entries(x.style||{}).filter(([k])=>['size','font','eastAsianFont','color','bold','italic','char_spacing_pt','line_spacing'].includes(k)))}));
 const byId=new Map((elements||[]).map(e=>[e.id,e]));
 const glyphSizeRequirements=compactDiffs.filter(d=>d.type==='glyph_size'&&Number(d.expected_height)>Number(d.actual_height)*1.2&&d.id&&byId.has(d.id)).map(d=>{
  const e=byId.get(d.id),box=e.box||[0,0,0,0],position=compactDiffs.find(p=>p.type==='position_or_size'&&p.id===d.id),actual=position?.actual_box||[],expected=position?.expected_box||[],ratio=Number(d.expected_height)/Math.max(1e-6,Number(d.actual_height)),scale=Math.min(1.5,ratio*.9),size=Number(e.style?.size)||12;
  const dx=actual.length===4&&expected.length===4?(expected[0]+expected[2]/2)-(actual[0]+actual[2]/2):0;
  const dy=actual.length===4&&expected.length===4?(expected[1]+expected[3]/2)-(actual[1]+actual[3]/2):0;
  return {id:d.id,target_text:d.expected_text||d.text||'',current_size:size,min_size:Math.min(96,Math.round(Math.max(size+.5,size*scale)*10)/10),expected_height:d.expected_height,actual_height:d.actual_height,min_box_height:Math.round(Math.max(Number(box[3])||0,Number(d.expected_height)*1.1)*10000)/10000,box_center_delta:[Math.round(dx*10000)/10000,Math.round(dy*10000)/10000]}
 });
 return {scope_ids:scopeIds,measured_differences:compactDiffs,strategy,rejected_candidates:rejectedCompact,previous_feedback:String(previous||''),history,last_modified_round,playbook,
  correction_rules:['每轮处理整页所有可修复差异，可同时返回多个对象的联合修改。','每个文字框只放一条视觉行；原稿有换行或一行放不下时必须由独立文本框承载下一行，禁止同框换行或自动折行。重建阶段负责逐行拆框，校准阶段遇到多行结构必须报告并要求重建。','同一项目符号列表的所有行必须对齐首字左边缘，并统一字体家族、字号、字重和颜色；不得保留OCR估算造成的横向缩进或字号抖动。','每次必须先逐项检查history中上一轮的previous_parameters、rendered_effect、实测前后分数和未解决差异；说明哪些参数有效并保留，哪些无效或造成回归并调整，不能脱离上一轮参数重新猜。','本轮每条correction.reason简要指出它回应的实测差异，以及相对上一轮对应对象参数具体保留或改变了什么；不得重复被实测否决的参数。','流程图上下叠放的文字不能只增大字号：必须同时放大文本框高度并依据字形框位置移动，保证上下行之间不重叠。','颜色只按给出的原稿/实渲染色值修正，不得改动没有颜色差异的对象。','对glyph_size_requirements中的对象，候选字号至少达到min_size，文本框高度至少达到min_box_height，并按box_center_delta校正框中心；真实渲染仍是最终验收。'],glyph_size_requirements:glyphSizeRequirements};
}
async function aiCalibrateDeck(eligibleIds=null){
 const gate=await stageRepairCheck();const blockers=gate.pages.flatMap(p=>p.issues).filter(i=>['missing_native_object','duplicate_native_object','background_residual_risk','text_overflow','unexpected_line_break'].includes(i.issue_type));if(blockers.length){if(state.optimizationRun){state.optimizationRun.running=false;state.optimizationRun.fatalError=true}$('aiCalibrate').textContent='启动样式诊断';$('aiQAResult').textContent='已停止AI参数校准：请先生成并验收结构/几何修复候选。';return false;}
 const eligibleSet=eligibleIds?new Set(eligibleIds):null;
 const slides=state.project.slides.filter(s=>(!eligibleSet||eligibleSet.has(s.id))&&!state.optimizationRun?.disabled?.[s.id]&&cleanPairCurrent(s)&&s.elements.some(e=>['text','formula'].includes(e.kind)&&e.text?.trim()));
 const results=[];state.calibrationDeck=results;
 if(!slides.length)return false;
 const run=state.optimizationRun;
 $('aiQAResult').textContent=`DeepSeek 正在整体校准 ${slides.length} 页（最多3页并发；每页完整检查所有差异）`;
 const propose=async(s,index)=>{
  if(run){run.pageStatus=run.pageStatus||{};run.pageStatus[s.id]='DeepSeek正在分析本页原稿与实渲染'}
  renderQAImages();
  try{const run=state.optimizationRun;let result=null,feedback=run?.feedback?.[s.id]||'',hasChange=false;
   const memory=run?.memory||{history:[],playbook:[]},recent=(memory.history||[]).filter(x=>x.slide_id===s.id).slice(-80),learned=(memory.playbook||[]).filter(x=>x.slide_id===s.id).slice(-6);
   // Whole-slide OCR boxes jitter between exports. Reject an exact proposal
   // that failed on measured score instead of allowing unrelated OCR changes
   // to make the repeated parameters appear novel.
   const persistedRejected=recent.filter(x=>x.candidate_applied&&['no_improvement','rollback'].includes(x.outcome)&&Number.isFinite(Number(x.before))&&Number.isFinite(Number(x.after))&&Number(x.after)<=Number(x.before)).flatMap(x=>x.corrections||[]);
   if(run&&persistedRejected.length)rememberRejectedCalibration(run,s.id,persistedRejected);
   const measuredDiffs=(state.lastReport?.visual_comparisons||[]).find(c=>c.slide_id===s.id)?.objective_score?.differences||[];
   if(run){run.roundDiffsBefore=run.roundDiffsBefore||{};run.roundDiffsBefore[s.id]=JSON.parse(JSON.stringify(measuredDiffs))}
   // Every proposal covers the full slide. Include all measurable defects and
   // their linked objects so DeepSeek can coordinate several edits in one pass.
   const scopeDiffs=measuredDiffs.filter(d=>deepSeekCalibratableTypes.has(d.type));
   const editableById=new Map((s.elements||[]).map(e=>[e.id,e]));
   const scopeEnriched=scopeDiffs.map(d=>{const target=editableById.get(d.id)||editableById.get((d.objects||[]).find(id=>editableById.has(id)));const enriched={...d};if(target){enriched.editable_box=[...(target.box||[])];enriched.editable_style={...(target.style||{})}}if(d.type==='text_overlap')enriched.editable_boxes=Object.fromEntries((d.objects||[]).filter(id=>editableById.has(id)).map(id=>[id,editableById.get(id).box]));return enriched}).sort((a,b)=>differencePriority(b,s.elements||[])-differencePriority(a,s.elements||[]));
   const scopeIds=[...new Set([...differenceObjectIds(scopeEnriched),...(s.elements||[]).filter(e=>['text','formula'].includes(e.kind)&&e.text?.trim()).map(e=>e.id)])];
   if(run&&!scopeEnriched.length){const unsupported=measuredDiffs.filter(d=>!deepSeekCalibratableTypes.has(d.type));run.stopped[s.id]=unsupported.length?'剩余差异属于缺字/内容/公式等结构问题，超出现有对象参数校准范围':'当前没有可由现有文字对象修复的实测差异';run.pageStatus=run.pageStatus||{};run.pageStatus[s.id]=run.stopped[s.id];logOptimization(`${s.id} · 转结构重建`,`${run.stopped[s.id]}；本页不会继续消耗DeepSeek校准轮次`,'error');return {slide_id:s.id,paused:true}}
   if(run){run.scopeDiffs=run.scopeDiffs||{};run.scopeDiffs[s.id]=JSON.parse(JSON.stringify(scopeEnriched))}
   for(let attempt=0;attempt<1;attempt++){
    if(run?.disabled?.[s.id])return {slide_id:s.id,paused:true};
    const next=(run?.proposalAttempts?.[s.id]||0)+1;if(run)run.proposalAttempts[s.id]=next;
    const strategies=['对照原稿与实渲染，仅检查字体、字号、颜色与字重；不修改坐标，不解决结构缺字'];
   const rejected=run?.rejectedCandidates?.[s.id]||[];
    feedback=compactCalibrationFeedback(feedback,measuredDiffs,recent,learned,rejected,strategies[(next-1)%strategies.length],scopeEnriched,(s.elements||[]).filter(e=>['text','formula'].includes(e.kind)).map(e=>e.id),s.elements||[]);
    const payload={...aiReq(),optimization_feedback:feedback,optimization_round:next};
    result=await post(`/api/projects/${pid()}/slides/${s.id}/calibrate-text`,payload);
    if(run?.disabled?.[s.id])return {slide_id:s.id,paused:true};
    const editableIds=new Set((s.elements||[]).filter(e=>['text','formula'].includes(e.kind)).map(e=>e.id));
    const invalidTargets=(result.corrections||[]).filter(c=>!editableIds.has(c.id)||!s.elements.some(e=>e.id===c.id&&String(e.text||'').replace(/\s+/g,'')===String(c.target_text||'').replace(/\s+/g,'')));
    if(invalidTargets.length){result.corrections=(result.corrections||[]).filter(c=>!invalidTargets.includes(c));result.issues=[...(result.issues||[]),`系统剔除了${invalidTargets.length}项ID/文字不匹配或非文字对象候选：${invalidTargets.map(c=>c.id).join(', ')}`]}
    const strippedProperties=validatePageWideCalibrationCandidate(result,scopeEnriched,s.elements||[]);
    if(strippedProperties)feedback={...(typeof feedback==='object'&&feedback?feedback:{}),previous_feedback:[feedback?.previous_feedback||'',`全页候选校验移除了${strippedProperties}项超出属性白名单或方向错误的参数；允许同时修复全页差异对象 ${JSON.stringify(scopeIds)}。坐标是归一化值，y增大代表向下、y减小代表向上。请基于图A/C的整页证据补全关联对象候选。`].filter(Boolean).join('\n')};
    const glyphRequirements=feedback?.glyph_size_requirements||[];
    // Geometry and inferred size overrides belong to the layout engine, never to model advice.
    const rejectedNow=(result.corrections||[]).filter(c=>calibrationCandidateWasRejected(c,run?.rejectedCandidates?.[s.id]||[]));
    if(rejectedNow.length){
     rememberRejectedCalibration(run,s.id,rejectedNow);
     result.corrections=(result.corrections||[]).filter(c=>!rejectedNow.includes(c));
     feedback={...(typeof feedback==='object'&&feedback?feedback:{}),previous_feedback:[feedback?.previous_feedback||'',`DeepSeek 本轮又返回了 ${rejectedNow.length} 个此前实测未提升的重复参数，系统已拒绝这些参数。失败参数：${JSON.stringify(rejectedNow.map(c=>({id:c.id,box:c.box,style:c.style||{}})))}。请保留尚未失败的新候选；若没有新候选，必须针对当前未解决差异换一种参数或明确说明现有文字对象无法修复。`].filter(Boolean).join('\n')};
     logOptimization(`${s.id} · 拦截重复候选`,`DeepSeek 重复提出 ${rejectedNow.length} 个此前实测未提升的参数；已剔除并请求新的可执行方案`,'error');
    }
    hasChange=(result.corrections||[]).some(c=>{const e=s.elements.find(x=>x.id===c.id);return e&&JSON.stringify({box:e.box,style:e.style||{}})!==JSON.stringify({box:c.box||e.box,style:{...(e.style||{}),...(c.style||{})}})});
    if(hasChange||attempt===0)break;
    if(result.corrections?.length)rememberRejectedCalibration(run,s.id,result.corrections)
    feedback={...(typeof feedback==='object'&&feedback?feedback:{}),previous_feedback:[feedback?.previous_feedback||'',`上次返回的候选无法改变任何PPT参数，原因可能是空 corrections 或参数与当前值相同。不要重复原答案。请重新对照C图实际渲染，尤其核查显式布局风险 ${JSON.stringify(result.layout_issues||[])}、同级样式组 ${JSON.stringify(result.peer_style_groups||[])}，对可见溢出/遮挡给出至少一个安全、不同且可执行的最小参数改动。若确实无可靠方案，issues中明确写出原因。`].filter(Boolean).join('\n')};
    logOptimization(`${s.id} · 候选无变化`,`DeepSeek 返回的候选没有改变PPT参数，正在带着具体风险再请求一次`,'error');
   }
    if(!hasChange&&run){
    // Keep the next pass page-wide; an empty proposal must not narrow scope.
    run.noCandidatePending=run.noCandidatePending||{};run.noCandidatePending[s.id]=true;
    run.feedback[s.id]=[run.feedback[s.id],`上一轮整页建议无法执行（未改变任何参数）。风险清单：${JSON.stringify(result.layout_issues||[])}；问题诊断：${JSON.stringify(result.issues||[])}。下一轮仍需全面复核全部差异，联合处理多个关联框，尝试不同且可执行的参数；不要缩窄到单个对象。`].filter(Boolean).join('\n')}
   if(run)run.pageStatus[s.id]=hasChange?'整页候选已生成，等待写入并真实渲染验证':'本轮没有可执行候选，下一轮仍按全页差异优化';
   if(run&&result){const correctionSummary=(result.corrections||[]).map(c=>({id:c.id,target_text:c.target_text,box:c.box,style:c.style||{},reason:c.reason}));logOptimization(`${s.id} · 第${run.rounds||0}轮差异反馈`,`实测仍待处理：${JSON.stringify((measuredDiffs||[]).slice(0,14))}；DeepSeek诊断：${JSON.stringify((result.issues||[]).slice(0,10))}；候选参数：${JSON.stringify(correctionSummary).slice(0,4200)}；候选是否改变参数：${hasChange?'是':'否'}`)}
   return {slide_id:s.id,ok:true,result,autoApplied:false}}
  catch(e){const billing=isDeepSeekBillingFailure(e);if(run)run.pageStatus[s.id]=billing?'DeepSeek余额不足，自动校准已停止':`校准失败：${e.message}`;return {slide_id:s.id,ok:false,fatal:billing,error:e.message}}
 };
 // Run a bounded worker pool: small decks (including this two-page deck) are
 // calibrated simultaneously, while larger decks cannot flood the API.
 let cursor=0;const workers=Array.from({length:Math.min(3,slides.length)},async()=>{while(cursor<slides.length){const index=cursor++,item=await propose(slides[index],index);results[index]=item;state.calibrationDeck=results.filter(Boolean);renderCalibrationDeck();renderQAImages()}});
 await Promise.all(workers);
 const proposed=results.filter(x=>x?.ok).length,failed=results.filter(x=>x&&!x.ok&&!x.paused).length;
 const requestFailure=results.find(x=>x&&!x.ok&&!x.paused);
 if(requestFailure){
  const run=state.optimizationRun;
  const billing=!!requestFailure.fatal;
  if(run){run.running=false;run.fatalError=billing?'DeepSeek API 余额不足（HTTP 402）；自动优化已停止。充值后再重新启动即可。':`DeepSeek 请求失败；自动优化已停止：${requestFailure.error}`}
  $('aiCalibrate').textContent='启动样式诊断';
  $('aiQAResult').textContent=billing?'DeepSeek API 返回 402：账户余额不足。本轮候选未应用，自动优化已立即停止，不会继续空转重试。请补充 API 余额后重新启动。':`DeepSeek 请求失败：${requestFailure.error}。本轮候选未应用，自动优化已停止，不会重复发起下一轮。`;
  logOptimization(billing?'DeepSeek余额不足 · 已停止':'DeepSeek请求失败 · 已停止',`${requestFailure.slide_id}：${requestFailure.error}。本轮没有应用候选，也没有发起下一轮。`,'error');
  renderQAImages();return false;
 }
 if(!proposed&&results.every(x=>x?.paused))return false;
 if(!proposed&&failed===0)return false;
 if(!proposed)throw Error(`DeepSeek 校准请求失败：${results.filter(x=>x&&!x.paused&&!x.ok).map(x=>`${x.slide_id} ${x.error}`).join('；')}`);
 let applied=0,applyFailures=0;
 if($('autoApplyCalibration')?.checked){
  for(const item of results.filter(x=>x.ok)){
   if(state.optimizationRun?.cancelRequested)break;
   if(state.optimizationRun?.disabled?.[item.slide_id]){item.paused=true;item.autoApplied=false;continue}
   const s=state.project.slides.find(x=>x.id===item.slide_id);if(!s){applyFailures++;continue}
   const elements=JSON.parse(JSON.stringify(s.elements||[]));let changed=false;
   for(const c of item.result.corrections){const e=elements.find(x=>x.id===c.id);if(!e||!['text','formula'].includes(e.kind))continue;
    const before=JSON.stringify({box:e.box,style:e.style||{}});if(c.box)e.box=[...c.box];e.style={...(e.style||{}),...c.style};
    if(before!==JSON.stringify({box:e.box,style:e.style||{}}))changed=true;
   }
   if(!changed){item.noChange=true;continue}
   try{await put(`/api/projects/${pid()}/slides/${s.id}`,{elements,title:s.title,body:s.body});item.autoApplied=true;applied++}catch(e){item.applyError=e.message;applyFailures++}
  }
  if(applied){if(run){run.lastCandidates=run.lastCandidates||{};for(const item of results.filter(x=>x.autoApplied))run.lastCandidates[item.slide_id]=item.result.corrections||[]}await loadKeep(state.sid)}
 }
 if(applyFailures&&!applied)throw Error(`候选自动写入失败：${results.filter(x=>x.applyError).map(x=>`${x.slide_id} ${x.applyError}`).join('；')}`);
 if(run)for(const s of slides)if(!run.disabled?.[s.id])run.pageStatus[s.id]=results.find(x=>x.slide_id===s.id)?.autoApplied?'整页联合候选已应用，等待实测':'本轮无修改，下一轮仍按全页差异优化';
 renderCalibrationDeck();
 $('aiQAResult').textContent=`DeepSeek 已整体校准 ${proposed} 页，调用失败 ${failed} 页；已应用整页候选 ${applied} 页，失败 ${applyFailures} 页。整页实测后决定保留候选或恢复最佳版。`;
 return applied>0;
}
function renderCalibrationDeck(){renderOptimizationLog()}

let manualContext=null;
function initManualDrawer(){
 const lab=$('tab-lab');lab.classList.remove('page');$('manualDrawerBody').append(lab);
 const panels=[...lab.querySelectorAll(':scope > .panel')];
 for(const [i,panel] of panels.entries())if(i!==2){const details=document.createElement('details');details.className='advanced-panel';const summary=document.createElement('summary');summary.textContent=i===1?'原稿定位与重新识别（可选）':'高级工具（可选）';panel.before(details);details.append(summary,panel)}
 $('labSaveRegions').textContent='保存调整草稿';$('labRefresh').textContent='重新读取';
 $('openManualRebuild').onclick=()=>run(()=>openManualDrawer());$('closeManualDrawer').onclick=()=>{$('manualDrawer').close();manualContext=null};
 $('manualPreview').onclick=()=>run(manualPreview);$('manualDrawer').addEventListener('cancel',()=>{manualContext=null});
}
async function openManualDrawer(message=''){
 pid();if(state.projectLoading)throw Error('页面正在加载，请稍后打开');
 const context={pid:pid(),sid:state.sid};manualContext=context;$('manualDrawerContext').textContent=context.sid+' · '+slide().title;
 $('manualDrawerMessage').textContent=message||'修改后点击「更新预览」，确认无误再保存本页。未勾选的文字不会被提升。';
 if(!$('manualDrawer').open)$('manualDrawer').showModal();
 $('labOriginal').src=murl(slide().images.design);$('labSlideBadge').textContent=state.sid;
 if(!state.labRegions.length){
  await labRefresh();
  if(manualContext!==context)return;
  if(!state.labRegions.length)state.labRegions=(slide().elements||[]).filter(e=>['text','formula'].includes(e.kind)).map(e=>({...JSON.parse(JSON.stringify(e)),editable:true,role:'custom'}));
 }
 labRenderRegions();await renderManualPreview();
}
function assertManualContext(){if(!manualContext||manualContext.pid!==pid()||manualContext.sid!==state.sid)throw Error('手动调整所属页面已改变，请关闭并重新打开')}
function focusManualRegion(index){
 if(index<0)return;ocrPage=Math.floor(index/ocrPageSize);$('ocrSearch').value='';filterOCR();
 const row=$('labRegions').querySelector(`[data-labrow="${index}"]`);row?.scrollIntoView({block:'nearest'});row?.querySelector('.lab-txt').focus();
}
async function renderManualPreview(){
 const stage=$('manualPreviewStage'),s=slide();stage.innerHTML=`<img src="${murl(s.images.clean||s.images.design)}" alt="当前页背景">`;
 await ensureProjectFonts({id:pid(),slides:[{elements:state.labRegions}]});
 for(const r of state.labRegions.filter(r=>String(r.text||'').trim())){
  const st=r.style||{},b=r.box,n=document.createElement('div');n.className='manual-preview-text'+(r.editable&&r.role!=='ignore'?'':' pending-candidate');n.dataset.regionId=r.id;n.title='点击定位并编辑该文字框';
  Object.assign(n.style,{left:b[0]*100+'%',top:b[1]*100+'%',width:b[2]*100+'%',height:b[3]*100+'%',fontSize:Number(st.size)/9.6+'cqw',fontFamily:browserFontFamily(r,st.font||state.project.design.font),fontWeight:st.bold?'700':'400',color:csshex(st.color||state.project.design.ink)});
  const line=document.createElement('span');line.textContent=r.text;const marker=document.createElement('span');marker.className='baseline-marker';line.append(marker);n.append(line);stage.append(n);
  n.onclick=()=>focusManualRegion(state.labRegions.indexOf(r));
  if(!r.editable||r.role==='ignore'){const badge=document.createElement('span');badge.className='candidate-status';badge.textContent=candidateStatusLabel(r);n.append(badge)}
  if(r.text_layout?.baseline_pt!=null){const base=stage.getBoundingClientRect(),delta=r.text_layout.baseline_pt*base.width/960-(marker.getBoundingClientRect().top-base.top);line.style.display='inline-block';line.style.transform=`translateY(${delta}px)`}
 }
}
async function manualPreview(){
 assertManualContext();state.labRegions=await reflowOCRTextBoxes(labReadRows());assertManualContext();
 for(const r of state.labRegions){if(!(r.text_layout?.layout_warning||[]).some(w=>w.startsWith('layout_conflict:'))){r.layout_review_reason=null;if(!r.ocr_conflicts?.length){r.review_required=false;r.review_reason=null}}}
 labRenderRegions();await renderManualPreview();
 const bad=state.labRegions.filter(r=>(r.text_layout?.layout_warning||[]).some(x=>x.startsWith('layout_conflict:')));
 $('manualDrawerMessage').textContent=bad.length?`还有 ${bad.length} 个布局冲突：${bad.map(r=>r.text.slice(0,30)).join('；')}。请调整或取消勾选。`:'预览已更新。确认后保存当前页，背景保持已上传的无字图。';
}
async function labApply(){
 assertManualContext();await manualPreview();assertManualContext();
 const regions=state.labRegions,rows=regions.filter(r=>r.editable&&r.role!=='ignore'&&r.text.trim());
 if(!rows.length)throw Error('请至少勾选一个有效文字框');
 const bad=rows.filter(r=>(r.text_layout?.layout_warning||[]).some(x=>x.startsWith('layout_conflict:')));
 if(bad.length)throw Error(`请先修正或取消勾选 ${bad.length} 个冲突文字框：${bad.map(r=>r.text.slice(0,25)).join('；')}`);
 const id=pid(),sid=state.sid,s=slide();if(!cleanPairCurrent(s))throw Error('请先上传当前页配对的无字背景图');
 await put(`/api/projects/${id}/slides/${sid}/regions`,{regions});assertManualContext();
 const preserved=(s.elements||[]).filter(e=>!['text','formula'].includes(e.kind));
 const edited=rows.map(r=>{const e={...r,kind:r.kind||'text',role:'custom'};delete e.logical_id;if(['title','body'].includes(e.id))e.id='manual_'+e.id;return e});
 await put(`/api/projects/${id}/slides/${sid}`,{elements:[...preserved,...edited],title:s.title,body:s.body});
 await post(`/api/projects/${id}/slides/${sid}/confirm-reconstruction`);state.autoCandidateSid=null;state.autoCandidateKind=null;
 $('manualDrawer').close();manualContext=null;await loadKeep(sid);await refreshReconstructionProgress();
 $('autoRebuildResult').textContent=`${sid} 已通过手动调整保存 ${rows.length} 个文字框；沿用已上传背景。`;toast('已保存本页手动调整，可继续处理下一页');
}

// ----- v0.2 Reconstruction Lab -----
const labEndpoint=()=>`/api/projects/${pid()}/slides/${state.sid}`;
const labUrl=(name)=>`/lab/${pid()}/${state.sid}/${name}?rev=${Date.now()}`;
let ocrPage=0; const ocrPageSize=15;
function filterOCR(){
 const q=($('ocrSearch')?.value||'').trim().toLowerCase();
 const rows=[...document.querySelectorAll('[data-labrow]')];
 const matches=rows.filter(n=>n.querySelector('.lab-txt').value.toLowerCase().includes(q));
 const pages=Math.max(1,Math.ceil(matches.length/ocrPageSize));ocrPage=Math.min(ocrPage,pages-1);
 rows.forEach(n=>n.style.display='none');matches.slice(ocrPage*ocrPageSize,(ocrPage+1)*ocrPageSize).forEach(n=>n.style.display='');
 $('ocrCount').textContent=`显示 ${matches.length} / ${rows.length} 个文字框`;
 $('ocrPage').textContent=`${ocrPage+1} / ${pages}`;
 $('ocrPrev').disabled=ocrPage===0; $('ocrNext').disabled=ocrPage>=pages-1;
}
function labRenderRegions(){
  const regions=state.labRegions||[];
  $('labRegions').innerHTML=regions.length?regions.map((r,i)=>{
    const b=r.box||[.05,.1,.5,.08];
    return `<div class="lab-region" data-labrow="${i}"><strong>${i+1}</strong><label title="勾选后保存为独立可编辑文字框" class="lab-select"><input type="checkbox" class="lab-editable" ${r.editable?'checked':''}>提升</label>`+
       `<input aria-label="识别文字" class="lab-txt" value="${esc(r.text||'')}">`+
       `<select class="lab-role"><option value="custom" ${r.role==='custom'||!r.role?'selected':''}>普通文本</option><option value="title" ${r.role==='title'?'selected':''}>规划标题</option><option value="body" ${r.role==='body'?'selected':''}>规划正文</option><option value="ignore" ${r.role==='ignore'?'selected':''}>忽略</option></select>`+
       b.map((v,j)=>`<input class="lab-coordinate" data-col="${j}" aria-label="${['x','y','w','h'][j]}" type="number" step="0.001" value="${Number(v).toFixed(9)}">`).join('')+
       `<button class="btn small danger lab-delete" title="移除">×</button><div class="lab-style-fields"><label>字号(pt)<input class="lab-size" type="number" min="6" max="96" step="0.1" value="${Number(r.style?.size)||18}"></label><label>字体<input class="lab-font" value="${esc(r.style?.font||state.project.design.font)}"></label><label>颜色<input class="lab-color" type="color" value="#${r.style?.color||state.project.design.ink}"></label><label><input class="lab-bold" type="checkbox" ${r.style?.bold?'checked':''}>加粗</label></div><div class="lab-warning">${esc(r.layout_review_reason||r.review_reason||(r.text_layout?.layout_warning||[]).join('；'))}</div></div>`;
  }).join(''):'<p class="hint">暂无文字区域。点击 OCR 或「添加文字区」。</p>';
  $('labRegions').querySelectorAll('.lab-delete').forEach((btn)=>btn.onclick=()=>{labReadRows(false);const idx=Number(btn.parentElement.dataset.labrow);state.labRegions.splice(idx,1);labRenderRegions()});
  filterOCR();
  const boxes=$('labRects');
  boxes.innerHTML=regions.map((r,i)=>{
     if(!r.editable)return '';
     let b=r.box||[0,0,0,0];return `<div class="lab-detected" style="left:${b[0]*100}%;top:${b[1]*100}%;width:${b[2]*100}%;height:${b[3]*100}%">${i+1}</div>`;
  }).join('');
}
function labReadRows(validate=true){
  const prior=state.labRegions||[];
 const nodes=[...document.querySelectorAll('[data-labrow]')],manuallyMovedGroups=new Set();
 nodes.forEach((node,i)=>{const old=prior[i],box=[...node.querySelectorAll('[data-col]')].map(x=>Number(x.value));if(old?.source_group&&old.box?.length===4&&box.some((value,j)=>Math.abs(value-Number(old.box[j]))>=1e-6))manuallyMovedGroups.add(old.source_group)});
 state.labRegions=nodes.map((node,i)=>{
    const text=node.querySelector('.lab-txt').value,box=[...node.querySelectorAll('[data-col]')].map(x=>Number(x.value));
    const sameBox=prior[i]?.box?.length===4&&box.every((value,j)=>Math.abs(value-Number(prior[i].box[j]))<1e-6);
    return {...(prior[i]||{}),id:prior[i]?.id||`text_${String(i+1).padStart(3,'0')}`,text,
      role:node.querySelector('.lab-role').value,editable:node.querySelector('.lab-editable').checked,box,
      confidence:prior[i]?.confidence||0,style:{...(prior[i]?.style||{}),size:Number(node.querySelector('.lab-size').value),font:node.querySelector('.lab-font').value,color:node.querySelector('.lab-color').value.slice(1),bold:node.querySelector('.lab-bold').checked},text_class:prior[i]?.text_class,
      source_text:text===prior[i]?.text?prior[i]?.source_text:text,source_box:sameBox?prior[i]?.source_box:box,
      source_group:prior[i]?.source_group,layout_locks:manuallyMovedGroups.has(prior[i]?.source_group)?{...(prior[i]?.layout_locks||{}),x:true,y:true,w:true,h:true}:prior[i]?.layout_locks};
  });
  if(validate)for(const r of state.labRegions){
    if(!Number.isFinite(r.style.size)||r.style.size<6||r.style.size>96)throw Error(`文字框 ${r.id} 字号应在6–96pt之间`);
    const b=r.box;if(b.some(x=>!Number.isFinite(x))||b.some(x=>Math.abs(x)>10)||b[2]<=0||b[3]<=0)
      throw new Error(`文字框 ${r.id}（${r.text.slice(0,35)}）坐标无效：请填写有限的 X、Y 和大于0的宽高`);
  }
  return state.labRegions;
}
function labDisplayCandidate(exists=false){
  ['labMaskImg','labCandidateImg','labInspectionImg','labDiffImg'].forEach(id=>$(id).removeAttribute('src'));
  if(exists){$('labMaskImg').src=labUrl(`${state.sid}_mask.png`);$('labCandidateImg').src=labUrl(`${state.sid}_candidate.png`);$('labInspectionImg').src=labUrl(`${state.sid}_inspection.png`)}
}
async function labRefresh(){
  const s=slide(),id=state.sid;
  $('labSlideBadge').textContent=s.id+' · '+s.title.slice(0,32);
  $('labOriginal').src=s.images.design?murl(s.images.design):'';
  $('labResult').textContent='正在读取 '+s.id+' 的实验数据…';
  if(!s.images.design){$('labResult').textContent='请先在 03 编号上传中上传 '+s.id+'_design.png';state.labRegions=[];labRenderRegions();labDisplayCandidate(false);return;}
  const r=await api(labEndpoint()+'/reconstruction');
  if(id!==state.sid)return;
  state.labAvailable=r.engines;
  $('labOcrCapability').textContent='可用OCR：'+(r.engines.rapidocr?'RapidOCR ':'')+(r.engines.tesseract?'Tesseract ':'')+(!(r.engines.rapidocr||r.engines.tesseract)?'尚未安装；请安装 requirements-reconstruction.txt 或手工添加区域':'')+'；LaMa：'+(r.engines.simple_lama?'已安装':'未安装（Telea/NS 可直接使用）');
  state.labRegions=r.ocr?.source_valid===true?r.ocr.regions.map(x=>({...x,role:x.role||'custom',editable:!!x.editable})):[];
  $('labCoverage').textContent=`已确认 ${r.coverage.confirmed_editable_regions} / OCR ${r.coverage.ocr_text_regions} 区域；仍为位图 ${r.coverage.raster_text_regions_estimate}`;
  labRenderRegions();labDisplayCandidate(r.candidate_exists);
  $('labResult').textContent=r.ocr?.source_valid===false?'警告：设计图已经替换，旧 OCR 区域已弃用，请重新识别。':`已读取 ${state.labRegions.length} 个文字区域。`;
}
async function labDetect(){
  if(!slide().images.design)throw Error('请先上传完整设计稿');
  $('labResult').textContent='本机 OCR 处理中…';
  const r=await post(labEndpoint()+'/ocr?backend='+encodeURIComponent($('labBackend').value));
  state.labRegions=r.regions.map(x=>({...x,role:'custom',editable:false}));labRenderRegions();
  $('labResult').textContent=`${r.backend} 识别到 ${r.regions.length} 个文本区域。请逐项校对文字和坐标，并指定 title / body / custom，不要直接信任 OCR。`;
}
async function labAnalyzeStructure(){
  if(!slide().images.design)throw Error('请先上传完整设计稿');
  $('labResult').textContent='DeepSeek 正在识别文字区、原生形状和独立插画区域…';
  const r=await post(labEndpoint()+'/analyze-structure',aiReq());
  state.sceneProposal=r;
  const counts=r.objects.reduce((a,o)=>(a[o.kind]=(a[o.kind]||0)+1,a),{});
  const rows=r.objects.map((o,i)=>`<label class="scene-row"><input type="checkbox" class="scene-select" value="${i}"><span><b>${esc(o.kind)}</b> · ${esc(o.role)} · ${(o.confidence*100).toFixed(0)}%<small>${esc(o.text||'未识别文字 / 视觉对象')} · [${o.box.map(v=>Number(v).toFixed(3)).join(', ')}]</small></span></label>`).join('');
  renderProposal('sceneProposal',`<h3>页面结构草案 · ${r.objects.length} 个对象</h3><p>${esc(r.notice)}<br>文字 ${counts.text||0} · 面板 ${counts.rect||0} · 线/箭头 ${counts.line||0} · 插画区域 ${counts.image||0} · 本地图形候选 ${r.geometry_candidates||0}</p><div class="scene-rows">${rows||'<p class="hint">没有识别到对象。</p>'}</div><button class="btn primary" id="applySceneProposal" ${r.objects.length?'':'disabled'}>导入勾选对象供校对</button>`);
  $('applySceneProposal').onclick=()=>run(labApplyScene);
  $('labResult').textContent='结构草案已生成，没有自动修改项目。请勾选可信对象后导入。';
}
async function labApplyScene(){
  const chosen=[...document.querySelectorAll('.scene-select:checked')].map(n=>state.sceneProposal.objects[Number(n.value)]).filter(Boolean);
  if(!chosen.length)throw Error('请先勾选要导入的对象');
  const regions=labReadRows();
  const native=[];let assetCount=0;
  for(const [i,o] of chosen.entries()){
    if(o.kind==='text'){
      regions.push({text:o.text||'',role:o.role||'custom',box:o.box,confidence:o.confidence,editable:false});
      continue;
    }
    let kind=o.kind,asset;
    if(kind==='image'){
      const extracted=await post(labEndpoint()+'/extract-asset',{box:o.box,make_transparent:false});
      asset=extracted.filename;assetCount++;
    }
    const style={size:o.style.font_size||18,color:o.style.color||state.project.design.ink,
      fill:o.style.fill||state.project.design.accent,stroke:o.style.stroke||state.project.design.accent,
      strokeWidth:o.style.line_width||1,bold:!!o.style.bold,arrow:!!o.style.arrow};
    native.push({id:`vision_${String(i+1).padStart(3,'0')}`,kind,box:[...o.box],text:'',style,...(asset?{asset}:{})});
  }
  await put(labEndpoint()+'/regions',{regions});
  const s=slide();const previous=JSON.parse(JSON.stringify(s.elements||[]));
  if(assetCount)await loadKeep(s.id);
  state.elements=previous.concat(native);state.elsSid=s.id;state.selected=native[0]?.id||'title';
  if(native.length){renderEditor();assignTab('editor');toast(`已导入 ${regions.length} 个文字候选、${native.length} 个原生形状/图片对象。请校正后保存。`)}
  else{state.labRegions=regions;labRenderRegions();toast(`已加入 ${regions.length} 个文字候选；默认未勾选提升，请校对文字后选择需要可编辑的区域。`)}
}
async function labSave(){
  if(manualContext)await manualPreview();
  const regions=labReadRows();const r=await put(labEndpoint()+'/regions',{regions});labRenderRegions();
  toast('已保存 '+r.regions.length+' 个区域；不会改动项目文案或正式底图');
}
async function legacyLabApply(){
  const regions=await reflowOCRTextBoxes(labReadRows());state.labRegions=regions;labRenderRegions();
  const rows=regions.filter(r=>r.editable&&r.role!=='ignore');
  await put(labEndpoint()+'/regions',{regions});const s=slide();
  if(!rows.length)throw Error('请先添加/识别文字区域');
  const els=JSON.parse(JSON.stringify(s.elements||[])).filter(e=>!String(e.id).startsWith('ocr_')&&!['title','body'].includes(e.id));
  for(const r of rows){if(String(r.text||'').trim())els.push({...r,id:`ocr_${String(r.run_id||r.id).slice(-70)}`,kind:r.kind||'text',text:String(r.text),box:[...r.box]});}
  state.elements=els;state.elsSid=s.id;state.selected=els.find(x=>x.kind==='text')?.id||null;
  renderEditor();assignTab('editor');toast('已提升勾选文字。请检查重复文本与坐标，并点击「保存布局」');
}
async function labGenerate(){
  const regions=labReadRows().filter(r=>r.role!=='ignore'&&r.editable);
  await put(labEndpoint()+'/regions',{regions:labReadRows()});
  if(!regions.length)throw Error('请先勾选至少一个要去字的文字区域。为保护复杂信息图，OCR候选默认不勾选。');
  $('labResult').textContent='正在本机制作精细 Mask 和背景候选，请稍候…';
  const r=await post(labEndpoint()+'/clean-candidate',{
    regions,mask_mode:$('labMaskMode').value,method:$('labRepairMethod').value,
    padding:Number($('labPad').value),threshold:Number($('labThreshold').value)});
  $('labMaskImg').src=labUrl(r.mask);$('labCandidateImg').src=labUrl(r.candidate);$('labInspectionImg').src=labUrl(r.inspection);
  $('labResult').textContent=`候选生成完成：修复方式 ${r.report.method}，覆盖像素 ${r.report.mask_percent}% 。请放大人工检查，满意后再确认。`;
}
async function labAccept(){
  if(!confirm('确认将当前候选背景提升为正式 clean 图片？旧版会备份到实验目录。'))return;
  const r=await post(labEndpoint()+'/accept-clean');await loadKeep(state.sid);
  toast('审核通过，已写入 '+r.filename+'。可以进入 04 重建并导出。');
}
async function labRasterFirst(){
  if(!confirm('此操作会将设计原图复制为视觉底图，并清空本页已有文字/图形对象（旧对象会备份）。所有原有文字起初仍然是位图。确认继续？'))return;
  const r=await post(labEndpoint()+'/raster-first');
  await loadKeep(state.sid);toast(r.notice);assignTab('lab');await labRefresh();
}
function labCropBox(){return ['X','Y','W','H'].map(n=>Number($('labCrop'+n).value))}
async function labExtract(){
  const r=await post(labEndpoint()+'/extract-asset',{box:labCropBox(),make_transparent:$('labMatte').checked});
  await loadKeep(state.sid);toast('已提取独立图片素材 '+r.filename+'（内部像素非原生可编辑）');
}
async function labCompare(){
  const r=await post(labEndpoint()+'/compare-render');$('labDiffImg').src=labUrl(r.diff);
  $('labResult').textContent=`全局平均像素差（仅调试用）：${r.mae_0_255} / 255。字体、行距及色彩渲染也会带来差异；不能作为自动通过标准。`;
}
function setupLabCrop(){
  const area=$('labCanvas');let anchor=null;
  const xy=ev=>{const b=area.getBoundingClientRect();return [Math.max(0,Math.min(1,(ev.clientX-b.left)/b.width)),Math.max(0,Math.min(1,(ev.clientY-b.top)/b.height))]};
  area.addEventListener('pointerdown',ev=>{if(!slide()?.images.design)return;ev.preventDefault();anchor=xy(ev);area.setPointerCapture(ev.pointerId)});
  area.addEventListener('pointermove',ev=>{if(!anchor)return;const end=xy(ev);const box=[Math.min(anchor[0],end[0]),Math.min(anchor[1],end[1]),Math.abs(end[0]-anchor[0]),Math.abs(end[1]-anchor[1])];
    $('labCropVisual').innerHTML=`<div class="lab-crop-box" style="left:${box[0]*100}%;top:${box[1]*100}%;width:${box[2]*100}%;height:${box[3]*100}%"></div>`;
  });
  area.addEventListener('pointerup',ev=>{if(!anchor)return;const end=xy(ev);const b=[Math.min(anchor[0],end[0]),Math.min(anchor[1],end[1]),Math.abs(end[0]-anchor[0]),Math.abs(end[1]-anchor[1])];anchor=null;
    if(b[2]<.005||b[3]<.005)return;['X','Y','W','H'].forEach((n,i)=>$('labCrop'+n).value=b[i].toFixed(4));
  });
}

function setupStage(){const stage=$('stage');stage.addEventListener('pointerdown',ev=>{if(state.mode!=='mask')return;ev.preventDefault();const b=stage.getBoundingClientRect();const x=Math.max(0,Math.min(1,(ev.clientX-b.left)/b.width)),y=Math.max(0,Math.min(1,(ev.clientY-b.top)/b.height));state.drag={mode:'mask',x,y};stage.setPointerCapture(ev.pointerId)});
 window.addEventListener('pointermove',ev=>{
  const d=state.drag;if(!d)return;
  if(d.mode==='mask'){
    const b=stage.getBoundingClientRect(),x=Math.min(1,Math.max(0,(ev.clientX-b.left)/b.width)),y=Math.min(1,Math.max(0,(ev.clientY-b.top)/b.height));
    const rect=[Math.min(x,d.x),Math.min(y,d.y),Math.abs(x-d.x),Math.abs(y-d.y)];
    const el=$('maskDrag');el.style.display='block';Object.assign(el.style,{left:(rect[0]*100)+'%',top:(rect[1]*100)+'%',width:(rect[2]*100)+'%',height:(rect[3]*100)+'%'});return;
  }
  if(d.id){const e=state.elements.find(x=>x.id===d.id);if(!e)return;
    const dx=(ev.clientX-d.startX)/d.width,dy=(ev.clientY-d.startY)/d.height;
    e.layout_locks={...(e.layout_locks||{}),x:true,y:true};e.box[0]=Math.max(0,Math.min(1-e.box[2],d.box[0]+dx));e.box[1]=Math.max(0,Math.min(1-e.box[3],d.box[1]+dy));
    const node=$('stageElements').querySelector(`[data-el="${d.id}"]`);if(node){node.style.left=e.box[0]*100+'%';node.style.top=e.box[1]*100+'%';}renderProperties();
  }
 });
 window.addEventListener('pointerup',ev=>{const d=state.drag;if(!d)return;
  if(d.mode==='mask'){
    const b=stage.getBoundingClientRect(),x=Math.min(1,Math.max(0,(ev.clientX-b.left)/b.width)),y=Math.min(1,Math.max(0,(ev.clientY-b.top)/b.height));
    const r=[Math.min(x,d.x),Math.min(y,d.y),Math.abs(x-d.x),Math.abs(y-d.y)];if(r[2]>.005&&r[3]>.005){state.masks.push(r);renderEditor()}
    $('maskDrag').style.display='none';
  }
  state.drag=null;
 });
}
async function createDialogProject(){const f={title:$('newTitle').value.trim(),topic:$('newTopic').value.trim(),pages:Number($('newPages').value),style:$('newStyle').value,audience:$('newAudience').value};
 if(!f.title||!f.topic)throw Error('请填写项目名称和主题简介');let p=await post('/api/projects',f);await loadProject(p.id);assignTab('plan');toast('已创建空白逐页规划；项目名称和主题简介不会自动变成页面文案')}
function bindings(){
 $('newProject').onclick=()=>$('newDialog').showModal();
 $('cancelNew').onclick=()=>$('newDialog').close();
 $('newForm').addEventListener('submit',ev=>{ev.preventDefault();run(async()=>{await createDialogProject();$('newDialog').close()})});
 $('refreshProjects').onclick=()=>run(refreshProjects);
 initManualDrawer();
 $('tabs').querySelectorAll('[data-tab]').forEach(el=>el.onclick=()=>assignTab(el.dataset.tab));
 $('ocrSearch').oninput=()=>{ocrPage=0;filterOCR()};$('ocrPrev').onclick=()=>{ocrPage=Math.max(0,ocrPage-1);filterOCR()};$('ocrNext').onclick=()=>{ocrPage++;filterOCR()};
 // Environment diagnostics moved to advanced tab.
 $('btnExportTop').onclick=()=>run(async()=>{pid();assignTab('qa');await exportDeck('editable',false)});
 $('lockPlan').onclick=()=>run(lockPlan);$('saveSlide').onclick=()=>run(saveSlide);$('aiPlan').onclick=()=>run(aiPlan);$('aiStyle').onclick=()=>run(aiStyle);$('aiImagePrompt').onclick=()=>run(aiImagePrompts);
 $('saveDesign').onclick=()=>run(async()=>{await put(`/api/projects/${pid()}/design`,{bg:$('colorBg').value,ink:$('colorInk').value,accent:$('colorAccent').value,muted:$('colorMuted').value,font:$('designFont').value});await loadKeep(state.sid);toast('设计规范已更新')});
 $('downloadPrompts').onclick=()=>run(async()=>{await getPrompts();location.href=`/api/projects/${pid()}/prompts.zip`});
 $('copyCurrentPrompt').onclick=()=>run(async()=>{await navigator.clipboard.writeText($('promptDesign').value);toast('已复制当前页完整设计稿提示词')});$('copyCleanPrompt').onclick=()=>run(async()=>{await navigator.clipboard.writeText($('promptClean').value);toast('已复制当前页无字底图编辑提示词')});
 $('uploadOne').onclick=()=>run(uploadOne);$('uploadMany').onclick=()=>run(uploadMany);
 $('reviewImagePair').onclick=()=>run(reviewImagePair);$('refineCleanPrompt').onclick=()=>run(refineCleanPrompt);
 $('slotSlide').onchange=ev=>chooseSlide(ev.target.value);
 $('slotRole').onchange=()=>renderEverything();
 $('slotFile').onchange=()=>renderCurrentUpload();
 $('dropSlot').addEventListener('dragover',ev=>{ev.preventDefault();$('dropSlot').classList.add('drag-over')});
 $('dropSlot').addEventListener('dragleave',()=> $('dropSlot').classList.remove('drag-over'));
 $('dropSlot').addEventListener('drop',ev=>{ev.preventDefault();$('dropSlot').classList.remove('drag-over');const f=ev.dataTransfer?.files?.[0];if(!f)return;if(!/^image\/(png|jpeg|webp)$/.test(f.type)){toast('请选择 PNG、JPG 或 WEBP 图片',true);return}const transfer=new DataTransfer();transfer.items.add(f);$('slotFile').files=transfer.files;renderCurrentUpload()});
 $('batchFiles').onchange=ev=>$('batchHint').textContent=[...ev.target.files].map(x=>x.name).join('，');
 $('elementSelect').onchange=ev=>selectElement(ev.target.value);
 document.querySelectorAll('[data-add]').forEach(x=>x.onclick=()=>run(async()=>addElement(x.dataset.add)));
 $('applyElement').onclick=()=>run(async()=>applyElement());$('saveElements').onclick=()=>run(saveElements);$('layoutElements').onclick=()=>run(layoutCurrentElements);
 $('removeElement').onclick=()=>{state.elements=state.elements.filter(e=>e.id!==state.selected);state.selected=state.elements[0]?.id||null;renderEditor();toast('已从预览移除，记得保存布局')};
 $('editorModes').querySelectorAll('[data-mode]').forEach(x=>x.onclick=()=>{state.mode=x.dataset.mode;renderEditor()});
 $('makeClean').onclick=()=>run(async()=>{if(!state.masks.length)throw Error('先进入标注去字选区模式，在设计图上框选文字区域');const r=await post(`/api/projects/${pid()}/slides/${state.sid}/inpaint`,{rects:state.masks});state.labRegions=state.masks.map(b=>({box:b,text:'',role:'custom'}));assignTab('lab');$('labMaskImg').src=labUrl(r.mask);$('labCandidateImg').src=labUrl(r.candidate);$('labInspectionImg').src=labUrl(r.inspection);toast(r.warning);});
 $('analyzeVisual').onclick=()=>run(analyzeVisual);$('repairAutoRun').onclick=()=>run(stageRepairAutoRun);
 $('runQA').onclick=()=>run(runQA);$('importWPSRender').onclick=()=>run(importWPSRender);$('exportEditable').onclick=()=>run(()=>exportDeck('editable'));$('aiVisualQA').onclick=()=>run(aiVisualQA);$('aiCalibrate').onclick=()=>{const button=$('aiCalibrate');if(state.optimizationRun?.running){state.optimizationRun.cancelRequested=true;button.textContent='正在停止…';return}run(async()=>{if(!$('autoSupervise').checked||!$('autoApplyCalibration').checked)throw Error('请先开启“导出后自动视觉检查”和“自动应用候选”');try{await exportDeck('editable',true)}catch(e){if(state.optimizationRun){state.optimizationRun.running=false;button.textContent='启动样式诊断'}throw e}})};
 $('referenceOpacity').oninput=ev=>{const v=Number(ev.target.value);$('referenceValue').textContent=v+'%';$('refOverlay').style.opacity=state.mode==='editable'?String(v/100):'0'};
 $('testAPI').onclick=()=>run(async()=>{$('debugOutput').textContent=pretty(await post('/api/ai/test',aiReq()))});
 [['debugHealth','health'],['debugPlan','plan'],['debugImages','projects'],['debugQA','qa'],['debugJSON','json']].forEach(([id,name])=>$(id).onclick=()=>run(()=>debugSection(name)));
 $('batchCreate').onclick=()=>run(async()=>{const f=$('batchCSV').files[0];if(!f)throw Error('请先选择CSV');const d=new FormData();d.append('file',f);const r=await api('/api/batch/create',{method:'POST',body:d});await refreshProjects();$('batchOutput').textContent=pretty(r);toast('已创建 '+r.created.length+' 个项目');});
 $('exportBatch').onclick=()=>run(async()=>{const ids=[...document.querySelectorAll('.batch-check:checked')].map(x=>x.value);if(!ids.length)throw Error('请勾选项目');$('batchOutput').textContent='批量生成与合并中…';const r=await post('/api/batch/export',{project_ids:ids,mode:$('batchMode').value});$('batchOutput').textContent=pretty(r);if(r.url){const a=document.createElement('a');a.href=r.url;a.download='批量PPT导出.zip';a.textContent='下载批量导出 ZIP';a.className='btn primary';$('batchOutput').after(a)}toast('批量任务完成：成功 '+r.success.length+'，失败 '+r.errors.length);});
 $('autoRebuildPage').onclick=()=>run(autoRebuildSlide);$('confirmAutoRebuild').onclick=()=>run(()=>state.autoCandidateSid===state.sid?confirmAutoRebuild():Promise.reject(Error('当前页没有待确认的 Agent 候选，请先运行当前页 Agent')));$('finishReconstruction').onclick=()=>run(()=>state.autoStageRetry?state.autoStageRetry():finishReconstructionAndEnterThirdStage());
 [['labRasterFirst',labRasterFirst],['labRefresh',labRefresh],['labDetect',labDetect],['labAnalyzeStructure',labAnalyzeStructure],['labSaveRegions',labSave],['labApplyTexts',labApply],['labGenerate',labGenerate],['labAccept',labAccept],['labExtract',labExtract],['labCompare',labCompare]].forEach(([id,fn])=>$(id).onclick=()=>run(fn));
 $('labSelectNone').onclick=()=>run(async()=>{labReadRows(false);state.labRegions.forEach(r=>r.editable=false);labRenderRegions();toast('已全部取消提升，请按需逐项勾选')});
  $('labAddRegion').onclick=()=>run(async()=>{labReadRows(false);state.labRegions.push({text:'',role:'custom',box:[.1,.1,.3,.055],confidence:0,editable:true});labRenderRegions()});
 setupLabCrop();
 setupStage();
}
async function init(){bindings();await refreshProjects();let remembered=null;try{remembered=sessionStorage.getItem('visualPPTStudio.qaProject')}catch{}const saved=state.projects.find(p=>p.id===remembered);if(state.projects.length)await loadProject(saved?.id||state.projects[0].id);else renderEverything();if(saved)assignTab('qa')}
window.visualPPTStudioQA={snapshot:()=>JSON.parse(JSON.stringify({project:state.project,sid:state.sid,elements:state.elements,fontStatus:state.fontStatus,selected:state.selected,projectLoading:!!state.projectLoading,reconstructionPages:state.reconstructionPages,lastReport:state.lastReport})),fontsReady:()=>document.fonts.ready,alignBaselines:alignGlyphBaselines};
window.addEventListener('resize',()=>requestAnimationFrame(alignGlyphBaselines));
init().catch(e=>toast('初始化失败：'+e.message,true));
})();
