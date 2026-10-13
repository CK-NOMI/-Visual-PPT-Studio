# 单行文字布局重构记录

## 阶段 0：真实基线

本地分支 `fix/text-layout-v2`，全部工作在本地进行。真实样本为
`workspace/6d57a8e7389d` 的 S001/S002，原始版本 revision 398。
`build/text-layout-v2/baseline/` 保存项目快照、逐 shape 参数、原生内容检查、
clean/native/full 三类 PPTX、PDF 和 LibreOffice 预览。

基线命令：

```sh
.venv/bin/python -m pytest -q --tb=short
.venv/bin/python scripts/text_layout_regression.py 6d57a8e7389d --label baseline
```

当前基线为 49 passed / 11 failed，完整日志在样本 baseline/pytest.txt。
失败包括旧测试使用固定导出文件名/伪造过期报告、空白初始文案、
OCR 多尺度 mock 不符合现有调用方式，以及评分函数对空场景报 KeyError。
后续分别修正测试契约和实现缺陷，不降低验收要求。

已确认根因：

- `web/app.js` 的拆分、确认、实验室提升删除原文两端空格；前端 Canvas
  测宽、缩字、reflow 与 `studio/pptx_native.py` 导出 reflow 各自决定参数。
- `_fit_single_line_font_size` 使用 `capacity * .80 / required`；前后端
  Latin advance 使用 `.92`，文本框宽度使用 `1.10`，后端还使用代理字体。
- `build` 可以改变已经保存的坐标、字号、标点，并把多行对象现场分框。
- `check_native_text` 的宽松归一化不能检查多余对象、精确空白、字号和坐标一致性。
- `detect_text`/前端对象上限截断未提供逐对象待复核理由。

最终验收环境改为 Mac WPS Office 12.1.28496。本机已安装 WPS，应用没有
AppleScript 脚本字典。界面确认存在「文件 → 输出为PDF格式／输出为图片」。
自定义文件对话框自动输入不可靠，保留人工导出与版本绑定的导入流程。
所有 LibreOffice 图片仅为辅助预览；WPS 视觉及编辑验收未完成，不能由分数替代。

## 实施约束

Python 统一布局在候选生成或显式编辑时发生；保存后的布局直接用于预览和导出。
TextBlock → TextLine → TextRun 保留原始文字、原始框、字符范围、稳定 ID、
空格归属、字体文件及度量、baseline、最终 box 和冲突。每个 run 为独立单行对象。
导出不缩字、不重新排版。旧数据先返回可审查的迁移候选并保留原始快照。
缺失字体只能使用明确记录的回退，不能把代理字体当成原字体测量。

## 验收方向更新

本阶段以真实网页渲染为视觉验收依据。暂停 WPS／PowerPoint GUI 自动化。
LibreOffice 仅作可选兼容性辅助；网页通过不代表 PPTX 在所有 Office 中已通过。
保留用户提供的两张 WPS 截图为历史证据，并不把它们升级为全页兼容性通过。

## 11 个既有失败的追踪

旧测试文件保持原样，没有删断言、降阈值或跳过失败。以下名称均来自阶段 0 日志。

| 测试 | 基线原因 | 当前处理 |
| --- | --- | --- |
| `tests/test_api.py::test_calibration_prompt_uses_rendered_effect_and_preserves_formula` | 主提示缺失任务和原文保护说明 | 恢复明确任务及保护原文说明，通过 |
| `tests/test_api.py::test_calibration_sends_labeled_original_and_current_render_every_call` | 主提示缺失保留字重及文本框判断要求 | 恢复说明，通过 |
| `tests/test_api.py::test_calibration_prompt_prioritizes_overflow_and_peer_style_consistency` | 只识别至少三行正文组，未处理来源明确的两个同级标题 | 增加由原图标题角色确认的同级标题组，通过 |
| `tests/test_api.py::test_api_plan_upload_export` | 旧固定下载名与版本化输出不兼容 | 固定名指向最新不可变版本；下载仍检查版本/哈希，通过 |
| `tests/test_api.py::test_calibration_agent_uses_current_render_and_only_suggestions` | 测试伪造报告只写 revision，没有 filename、artifact_sha256 或实际 PPTX | 仍失败；保留拒绝过期/无法追溯预览的真实保护，不接受伪造报告 |
| `tests/test_reconstruction.py::test_lab_manual_mask_candidate_accept_and_native_text` | 测试读取旧固定输出名 | 提供本地原子符号链接兼容旧名，通过 |
| `tests/test_reconstruction.py::test_render_fidelity_score_uses_measured_text_geometry_and_style` | 无场景评分调用 `{}` 触发 `p['slides']` KeyError | 允许独立图片评分时场景为空，通过 |
| `tests/test_reconstruction.py::test_render_fidelity_penalizes_text_box_width_and_height_mismatch` | 同上 | 同上，通过 |
| `tests/test_reconstruction.py::test_ink_density_detects_bolder_rendered_glyphs` | 同上 | 同上，通过 |
| `tests/test_renderer.py::test_native_shapes_and_raster_fallback` | 初始文案为空时导出器跳过两个已保存文本框，场景图不一致 | 空白原生占位框也保留，通过 |
| `tests/test_v03_regression.py::test_ocr_candidate_not_auto_selected` | 单尺度 `_rapidocr` mock 不随输入图变化，多尺度/分块调用产生五个不同位置候选，断言仍假定只有一个 | 仍失败；不把真实不同位置的重复标签去重成一个。新的测试分别验证多位置标签和完整工作流 |

当前保留的两项失败均在改动前存在。新增测试覆盖原文空格、字符范围、分框、
基线、真实字体回退、公式结构测量、布局幂等性、锁定、超宽冲突、精确 OOXML、
篡改/重复检测、纯候选 API、超过148个对象和保存重载。

## 阶段 1：统一布局

- `studio/text_layout.py` 为唯一布局计算端，维护 block/line/run、原稿证据框、
  Python 字符范围、空白归属、最终 box、advance、baseline 与冲突。
- `studio/fonts.py` 从字体文件和 TTC face 的真实名称建注册表，使用 HarfBuzz
  shaping/字形范围与 FreeType 行度量，记录字重、版本、文件 SHA256 和回退。
- 浏览器 FontFace 显式加载测量用的相同字体文件；等待 `document.fonts.ready`。
  CSS 只投影最终 box/size/tracking。基线标记注册到引擎提供的 baseline，
  不更改 x、字距或字号，也不使用 Canvas 再排版。
- 删除 JS Canvas 缩字、`.92` Latin advance、`1.10` 框宽和源宽缩字逻辑。
  删除 Python 二次空间 reflow、代理字体测量及 `.80` 静默缩字。
- 导出逐对象写入保存的数据；关闭自动换行和自动适配，保留 Latin/EA/CS。
  `check_saved_layout` 检查精确文字/空白、对象数、坐标、字号、边距、禁换行。
- 文本框容量取同一视觉行的可用区域，字形起点仍按真实 advance 连续计算；
  透明框重叠不等于字形重叠。强制锁定属性保持不变，冲突必须报告。
- 修复真实浏览器发现的加载/保存竞态：加载期间阻止保存和切页，异步请求有
  serial，保存后重载保持当前页。QA 还核对原始文字数量与内容，避免空页误过。
- 旧项目有显式「统一文字布局候选」按钮，检查后保存。真实样本的原始数据
  已保留在 baseline/source_project.json，迁移可逆。

## 一键浏览器回归

首次安装测试依赖和浏览器：

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
```

运行（若系统 Python 缺 Playwright，脚本自动使用项目 .venv）：

```sh
python scripts/qa_runner.py --browser --slides S001,S002
# 指定已有项目；视觉待复核项也使进程失败：
.venv/bin/python scripts/qa_runner.py --browser --project 6d57a8e7389d --slides S001,S002 --strict-visual
```

脚本把项目复制到报告目录下的隔离 workspace，启动临时本地服务和 Chromium。
真实点击加载、保存、切页、刷新、重新打开、下载 PPTX；保存每页完整网页和
幻灯片截图、DOM Range/Canvas ink/基线/CSS/字体加载与 CDP 实际字体信息。
另用两页隔离夹具测试图片上传、Agent候选、逐页确认和整套导出；该夹具明确
使用 OCR/语义响应录制值，不调用 DeepSeek，不声称验证模型识别准确率。

报告在 `workspace/<id>/build/browser_qa/<时间>/report.html`，同目录有 JSON、
按对象可点击的原稿/浏览器局部对照、原始/最终项目快照和 export.pptx。
独立指标包括内容/空白、CSS字号/位置、基线、行内字形间距、裁切、可见字形
重叠、字体回退、颜色/字高/原稿OCR缺失和背景对比度。旧评分仅为图片证据。
默认退出码检查布局/工作流/PPTX硬契约；`visual_status` 仍会独立显示
`requires_review`，不会以高总分抹去单个文字问题。`--strict-visual` 加强门禁。

## 尚需复核的原稿还原问题

S001/S002 的同份布局在浏览器和 PPTX 参数检查中可通过，但部分保存的旧 OCR
文字、字色、字高仍与原稿有差异。S002 左上圆形编号中的 `T` 与 `1` 是两份
不同旧 OCR 对象，可见字形确实重叠，不能靠移动其中一个称为已修复。
字号/颜色差异与识别内容准确性保留在逐对象报告中；它们不是导出器再次缩字。
公式采用独立 MathML 结构度量和 OMML 导出，复杂公式仍标记度量不确定。


## 最终验证记录（2026-10-12）

- 全套原有与新增测试：`83 passed, 2 failed`（85 项，47.04 秒）。
  两个失败均为上表保留的既有失败；初始 11 项失败中 9 项已修复。
  新增 25 项测试全部通过；没有修改原有测试文件。
- 测试日志：`workspace/6d57a8e7389d/build/text-layout-v2/final-tests.txt`。
- 真实项目已从 revision 398 迁移到 399；迁移前完整备份为
  `build/text-layout-v2/project_before_v2_r398_1791802966.json`。
- 用户原始「测试1」四张图片与当前 S001/S002 的 design/clean 在缩略图逐像素
  比较中完全一致（文件编码/哈希不同）；浏览器测试仍使用保存的这两页真实样本。
- 新 OCR 字号估算按原图可见字形高度、实际字体字形高度和 540pt 页面高度计算，
  对图片分辨率归一化，保存估算证据；旧样本保存字号不被重新估算。
- PPTX 精确校验另检查单段落、Latin/EA/CS 字体槽和粗体/斜体；字体被篡改的
  负例必须失败。网页通过不等于 Office/WPS 兼容性已经验证。
- 本地阶段提交：阶段 0 `da628aa`，阶段 1 `efe4502`。
  浏览器回归单独提交；用户正在修改的前端导航与组件重构保留在工作区。

- 使用用户最新前端的完整浏览器回归：
  `workspace/6d57a8e7389d/build/browser_qa/20261012-193550/report.html`。
  S001 63 个、S002 102 个原生文字对象：布局失败 0/0；真实切页/保存/刷新/
  重开和隔离两页上传/自动重建/确认/导出通过，PPTX 精确检查通过；无浏览器
  JS 错误。视觉待复核 48/70 个对象（同对象可含多项指标），S002 有 1 个
  字形重叠对象，故 `visual_status=requires_review`，并未判定整页视觉通过。


## 后续：源 OCR 冲突防护

用户要求保留已认可 baseline，并暂不重跑整套测试。本次继续修复来源问题：

- 嵌套 OCR 去重仅在片段文字确实属于完整文字时执行。`T` 与 `1` 这类
  矛盾观察保留双方原文、框及置信度，标记 `review_required/ocr_conflicts`。
- 判断在按中英类型拆分前执行，防止共享来源框的同行 runs 被当成冲突。
  空间分离的重复 Token 标签不被合并。自动重建/残字提升均跳过待复核冲突。
- 待复核清单明确显示另一种识别结果；人工勾选仍可显式决定采用哪个结果。
- 真实 S002 修复候选在 `build/text-layout-v2/source-conflict-candidate/`。
  仅移除局部对照已确认错误的 `ocr_004:T`，保留 `ocr_005:1`、其字号与其他
  页面布局。原项目与 baseline 未覆盖。JSON 保存完整删除对象及来源版本；
  候选 PPTX 精确结构检查通过，未重新运行浏览器或 WPS/Office 渲染。
- 添加两个 OCR 冲突/嵌套片段测试，依用户要求本次未执行；此前83通过/2失败
  的结果属于前一次完整运行，不能当成本次补丁已验证的数量。


## 修复 Agent 区域越界（2026-10-12）

真实 S001 `text_001` 的38.3pt标题，统一字体测量后框宽1.118098958，
引擎已报告溢出，但 `persist_regions` 错用原图裁剪的0–1边界验证最终文字框，
导致全批保存失败。现已将两种几何分开：原图裁剪继续严格检查；v2布局允许
保存有限且合理的越界坐标作为诊断候选，同时禁用自动提升并标待复核。
不夹紧坐标、不缩字号、不静默丢弃对象。非法数字仍拒绝，错误包含对象ID/文字。
前端对布局冲突显示复核原因；Agent失败会清理确认状态、恢复按钮，并显示
实际失败原因，不停留在「处理中」。本地服务已重启，JS版本更新以刷新缓存。
本次针对布局契约/浏览器诊断测试15项通过；未重跑整套/AI或WPS渲染。


## 第二步手动调整抽屉

独立OCR导航已撤下，第二步「手动调整」打开右侧抽屉，复用原有识别区域和
存储接口。Agent异常和确认时的选中对象冲突可直接进入人工处理；支持新增、
删除、取消提升、文字/字号/字体/粗细/颜色/坐标编辑、更新预览和保存草稿。
点击预览文字可定位编辑行。确认保存通过同一个布局端、页面保存接口和第二步
确认接口；保留其他页与上传背景。已上传clean默认直接沿用，素材裁剪和背景
修复折叠为可选高级工具。修正后的过期冲突提示被清除；未修正的选中冲突明确
指出对象，允许取消勾选继续保存。保存后留在第二步。

真实网页验证在隔离项目副本上执行：打开64区域、改标题为26pt、预览、保存、
刷新均通过；另一页及上传图片不变，无JS错误，原项目未修改。证据在
`build/manual-drawer-qa/report.json`与`drawer.png`。一键qa_runner也增加抽屉
工作流检查。本次未重新运行整套AI/浏览器视觉评分。
