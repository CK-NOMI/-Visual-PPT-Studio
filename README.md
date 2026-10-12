# v0.4 · Image2Editable 自动化工作流（2026-10-10）

**推荐先看：** [真实图片测试与操作说明](docs/TRANSFORMER_BENCHMARK.md) 和 [升级修复记录](docs/V03_CHANGELOG.md)。

**当前排版与导出问题及截图证据：** [当前问题清单](docs/CURRENT_ISSUES.md)。

这个版本针对用户提供的单页 `Transformer 结构详解` 设计稿调试。与 v0.2 的根本差异是：

- **自动为主、用户把关**：常用流程简化为“规划 → 上传与自动重建 → 检查与导出”；DeepSeek + OCR 负责理解、定位和校准，底图/文字候选仍由用户预览确认。
- **新增“原图基线→选择性提升”**：不会一次性把密集的信息图中的所有文字 OCR 重建。需要精细控制时，可在“更多工具 → OCR与背景修复”建立保真基线，再勾选需要真正可编辑的文字，局部去字并重建。
- **OCR 候选默认不勾选**：防止噪声 OCR/中文乱码一次性污染整张 PPT；仅经人工选择的区块能生成可编辑文字对象。保留未提升内容为栅格文字并明确警告。
- **主编译器切换为 python-pptx**：无须 Node/npm 即可启动、导出原生 PowerPoint。PptxGenJS 作为 `PPT_RENDERER=node` 可选后端保留。
- **禁止过期底图及过期预览**：设计图变化会使旧清洁底图失效；页面变化会使前一次差异分析失效。每次导出都会清理旧预览。
- **导出后逐页自检**：真实 Office 预览生成后，自动与对应设计稿生成像素差热图，分别报告整页、原生文字框和其余背景的差异；热图只用于定位变化，不作为审美评分或自动通过门槛。
- **双图像工作流与 DeepSeek 监督**：按页生成“含字设计图 / 无字底图”成对 Prompt；手动上传两张图后可让 DeepSeek 检查残字、构图漂移和非文字损伤，并基于真实 PPT 渲染提出可审核的文字校准建议。
- **配对安全**：clean 记录生成它时对应的 design 哈希。上传顺序不影响安全；design 更换后旧 clean 会被拒绝用于混合导出，须重新配对。
- **局部修复检查**：保存原图/候选的放大对照、掩码、覆盖率和像素差热图，背景候选必须审核后才能作为正式底图。
- **真实样本**：`samples/S001_transformer_reference.png`（用户提供）、`samples/seed_transformer.py`、`samples/S001_ocr_diagnostic.png`，附可编辑结果 PPTX 和真实 LibreOffice 预览。

**一键测试：** 在项目根目录运行 `PYTHONPATH=. .venv/bin/pytest -q`（当前 34 项回归测试；含 LibreOffice 真实渲染、原生 Office Math 公式和隔离式重建闭环）。

**可编辑性声明：** 某个可编辑 PPTX 并不意味着该页所有文本可编辑。真实图片演示仅把右上方一条标题提升成原生文本，其余内容仍属于栅格背景。复杂背景中的局部去字会产生伪影，必须人工审核。没有任何全自动、无损拆解复杂 PNG 成 PPT 原生元素的虚假保证。

---

# Visual PPT Studio — 双阶段视觉设计 + 可编辑 PPTX（本地开发预览版 v0.4）

**这是一套实实在在可运行的原型，不是只有架构图或 Prompt。** 中文 Web 界面包含 6 个核心模块的独立调试入口、单页选择式上传/批量严格命名上传、可编辑元素编辑、重建实验室（OCR/文字掩码/候选背景审核）、真实 PPTX/PDF/PNG 导出、结构化 QA 和 DeepSeek 可选 API。图片设计由你先在 **ChatGPT 网页版** 手工完成，然后上传；**程序不会调用任何 OpenAI 图片 API**。

> 目标：**各种主题、每月10–100套、图像设计质量与PowerPoint可编辑性并重。** 本版是单机 MVP；不是生产级 SaaS，复杂图片到原生形状的自动重建仍在探索阶段。

## v0.4 工作流与边界

主导航收敛为三步：**规划 → 上传与自动重建 → 检查与导出**。批量上传、素材质检、OCR、文字/公式/图形编辑和调试收在“更多工具”或页面内折叠的高级区。

新建项目时填写的项目名称和主题简介只作为整套演示文稿的元数据，不会自动变成第一页/第二页文案；预建页数只创建空白页面框架。逐页标题和正文需要在内容规划中单独确认。完整设计稿提示词要求图片直接呈现页面文字；只有第二阶段 `clean` 提示词才要求去除文字。

自动化默认开启一轮导出后视觉监督与文字排版校准；用户可以关闭“自动应用一轮”，让 DeepSeek 只出建议、由用户逐页确认。Agent 遇到低置信度识别、图片错配或可能损伤背景时会停止并交还控制权，不会静默覆盖正式内容。

上传同页 `design` 与 `clean` 后，系统先做哈希配对检查、DeepSeek 视觉理解和 OCR。通过审核的 clean 会直接沿用，不会从 design 重新擦字；只有少量残字、无构图漂移/非文字损伤且坐标有效时，才从 **clean 本身**生成待确认的局部修复候选。修复候选以 design 和 clean 哈希共同校验；残字过多或模型返回异常时会安全停止。

导出真实 PPTX 后，字体/字号/位置/溢出校准默认自动应用一轮并重新导出；可以关闭自动应用，改为逐页人工审批。公式编辑使用 LaTeX 源码，导出为 PowerPoint 原生 Office Math（OMML），非截图。

- **所有新依赖均为可选**。不开 OCR 时仍能手工指定文本框；OpenCV 修复继续免费、本地执行；不消耗图像 API。
- **图片修复候选不会直接替换正式 clean**，必须点“确认候选”。旧版图层与编号素材继续兼容。
- **不是万能像素级 PPT 矢量化**：大面积复杂插画依然是独立可移动位图；只保证真实 PPT 原生文字/形状可编辑。
- 详细操作、扩展安装、局限见 [docs/RECONSTRUCTION_LAB.md](docs/RECONSTRUCTION_LAB.md)。

## 视觉结构识别

DeepSeek Vision 对当前设计图给出页面结构草案，识别文字块、矩形/椭圆/线条和插画区域；OpenCV 也会补充低置信度几何候选。OCR 区域结合结构角色和规划文本决定提升建议，低置信度内容留给人工。复杂插画仍是位图，普通裁剪不会补回遮挡像素。此流程使用 DeepSeek 视觉 API，不调用图像生成 API。

## 0. 启动（macOS / Linux）

需要 Python 3.11+、Node.js 20+。建议安装 LibreOffice（真实渲染和 PDF/PNG 预览用，缺少时依然能导出 PPTX）；程序兼容 `libreoffice` 和 `soffice` 两种可执行文件名。DeepSeek 功能可在调试中心临时输入 Key，或在被 `.gitignore` 忽略的 `.env` 中设置 `DEEPSEEK_API_KEY_FILE` 指向本地文本/RTF 密钥文件；密钥只读入进程内存，不会写入项目数据。

```bash
unzip VisualPPTStudio_v0.2_Reconstruction_Lab.zip
cd visual_ppt_studio
bash start_mac_linux.sh
```

浏览器打开 **http://127.0.0.1:8765**。

如果是 Windows：安装 Python / Node.js，在解压后的目录运行 `start_windows.bat`。如果 `npm install` 下载依赖失败，请先确认 Node/npm 网络可达。Apple Silicon MacBook 也可以运行，不依赖 CUDA。

首次启动时安装 Python/Node 依赖，需要联网；实际手动生图 + 本地 PPT 导出流程可以在安装完后离线运行。

如果希望快速看到可运行示例，另开终端执行：

```bash
python samples/seed_demo.py
```

刷新网页，选择 **NEURAL FUTURES**。这是程序生成的测试图，不是 GPT Image 2.5 的图片，不能代表 GPT Image 的设计质量。

## 1. 工作流：ChatGPT 网页版手动生图

1. 新建项目只输入项目级名称、主题简介和预建页数；再在“规划”逐页填写标题、正文和用途，锁定后编号固定。
2. 在“上传与自动重建”分别上传当前页 `S001_design.png` 和 `S001_clean.png`。系统自动配对检查并运行 DeepSeek + OCR。
3. 点击“运行 Agent”，检查预览后确认；需要时可在“更多工具 → 编辑器”手动修改文字、公式与图形，低频修复工具位于页面折叠区。
4. 在“检查与导出”运行 QA 并真实渲染 PPTX。默认自动校准一轮；关闭自动应用后由你决定是否采用建议。
5. 批量创建和打包位于“更多工具 → 批量生产”。Image 图像 API 尚未接入，图片由你手动生成和上传。

### 上传命名规则

| 保存后的文件名 | 必要性 | 用途 | 上传方式 |
|---|---|---|---|
| `S001_design.png` | 强烈建议 | 含文字的完整视觉目标；栅格模式才整页插入 | 单页上传可用任意源文件名，系统自动按页码和类型命名 |
| `S001_clean.png` | 高保真混合模式建议必备 | 不含文字的背景图，作为可编辑 PPT 底图 | 单页上传可用任意源文件名，系统自动按页码和类型命名 |
| `S001_asset_01.png` | 可选 | 透明插画、图标、装饰，作为原生图片对象放置 | 单页上传可用任意源文件名，系统自动按页码和类型命名 |
| `S002_design.png` | 批量上传时必需 | 第2页完整设计稿 | 批量上传通过文件名识别页码和类型 |

单页上传时选择目标页和图片类型即可，原始文件名不限，程序自动保存为 `S001_design.png` 等。只有批量上传必须在文件名中包含页码和类型；程序不会根据上传顺序猜测。允许 PNG/JPEG/WebP，单文件<=25MB；推荐16:9，建议每页1920×1080或更高。

**不要**把含有标题的 design 直接用作混合可编辑背景！否则叠加原生文本时会出现重影。没有 clean 的页面，`editable` 会使用选定纯色底图；`raster` 才使用完整 design 图片并明确标记不可编辑。

## 2. 六个模块到底实现了什么？

| 模块 | 当前可运行功能 | DeepSeek API |
|---|---|---|
| ① 内容策划 | 固定页码、本地占位规划、逐页编辑/锁定、JSON方案、可选AI规划建议 | 可选 |
| ② 视觉总监 | 4套色彩预设、全局配色、逐页生图Prompt及ZIP、可选AI风格建议 | 可选 |
| ③ 视觉资产引擎 | 手工上传设计/无字/透明素材、自动归档与校验、可选实验局部去字 | **不需要** |
| ④ PPT 重建 | 原生文字、LaTeX→Office Math 公式、矩形、椭圆、线、图片和PPTX导出 | **不需要** |
| ⑤ QA | 本地规则/文本/公式核验、真实PPTX渲染、DeepSeek 字体字号溢出校准与视觉评审 | 可选 |
| ⑥ 批量 | CSV创建1–100个项目、按项目失败隔离、批量输出PPTX/PDF ZIP | **不需要** |

**目前尚不具备：**ChatGPT Image API 自动调用、完全自动的高保真图表/表格/复杂曲线拆分、图形组编辑、PowerPoint 高级动画、分布式任务队列和用户认证。复杂残字擦除、字体拟合及密集信息图文字校对仍需人工确认；自动校准只应用一轮受范围约束的建议，不保证审美完全复原。

## 3. DeepSeek 接入方法（可选）

可在网页 **调试中心** 填临时 Key 来测试（刷新后不会留在浏览器内存）。本机 `.env` 支持直接设置 Key，也支持只配置 Key 文件路径：

```bash
DEEPSEEK_API_KEY=your_actual_key_here
# 或：DEEPSEEK_API_KEY_FILE=/absolute/path/to/deepseek_api.rtf
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

不要把 `.env` 上传到 GitHub，更不要在公开聊天里粘贴密钥。所有项目元数据、PPTX和ZIP**不保存 API Key**。只有主动触发 AI 规划、双阶段提示词、图片配对审核、布局分析、校准或视觉 QA 时，相关文本/图片才会被发送到 DeepSeek；普通上传和导出不会把图片传到外部平台。

本程序默认使用 `deepseek-flash`，它是当前官方支持图像输入的 Flash 模型名。旧名 `deepseek-v4-flash` 仍可兼容路由，但官方已将旧版本退休并指向新版；模型名可在界面替换。模型路由会变化，使用前请核对 [DeepSeek Vision 文档](https://api-docs.deepseek.com/guides/vision/) 和 [官方更新日志](https://api-docs.deepseek.com/updates/)。

## 4. 模块独立测试和代码入口

```bash
python scripts/doctor.py
python -m pytest -q
python samples/seed_demo.py
python scripts/check_project.py <PROJECT_ID>
python scripts/export_project.py <PROJECT_ID> --mode editable
python scripts/export_project.py <PROJECT_ID> --mode raster
```

后端 Swagger API：`http://127.0.0.1:8765/docs`。也可单独启动：

```bash
python -m pip install -r requirements.txt
cd renderer && npm install && cd ..
python run.py
```

功能与文件入口：

| 文件 | 责任 |
|---|---|
| `studio/core.py` | 项目持久化、锁定、固定页码、资产验证、手工提示词 |
| `studio/ai.py` | DeepSeek的JSON/视觉适配器、建议生成、不自动覆写 |
| `studio/quality.py` | 几何越界、估算溢出/对比度、原生内容源检查 |
| `studio/server.py` | FastAPI路由、单页上传/批量创建、导出和真实预览 |
| `renderer/render.js` | PptxGenJS原生PPTX编译器 |
| `web/index.html / app.js / style.css` | 六模块工作台、可视化编辑、查看调试结果 |
| `tests/` | 无API自动化测试、严格编号/原生导出、隔离与资源测试 |
| `samples/seed_demo.py` | 生成3页离线测试项目和成对编号测试图片 |

## 5. 质量、可编辑性与真实性

`editable` 模式中标题、正文和添加的文本框将是真实 PowerPoint 文本对象，矩形/线/椭圆也是PowerPoint形状，插画仍是独立位图对象。该模式**不是**“全像素矢量化”。设计图只作视觉参照，实际位置由每页JSON规范控制。默认原生元素与设计稿可能不完全重合，须经过编辑和人工审核。

本地自动去字使用 OpenCV inpainting 或可选 LaMa，遇到复杂背景、渐变、文字覆盖主体会产生伪影；生产用途建议使用 ChatGPT 网页版再次编辑生成 `clean` 图。不把 OpenCV 结果描述为与原设计完全一致。

`quality.py` 的像素抽样/文本行数属于近似规则，不能证明专业排版的最终质量。`python-pptx` 的原生对象核验更可靠，但不能证明是否存在视觉遮挡。真正的审美校验仍需人工或手动开启 DeepSeek Vision 对比。

## 6. 开源项目调研（2026-10-10）

调研了 [Image2PPT](https://github.com/dlinjiade-debug/Image2PPT)、[px-image2pptx](https://github.com/JadeLiu-tech/px-image2pptx) 和 [ppt-screenshot-to-editable-pptx](https://github.com/NightSensingLab/ppt-screenshot-to-editable-pptx)。它们验证了 RapidOCR/PaddleOCR 文本框、局部掩码与修复、原生文本叠加、复杂视觉保留为图像对象，以及真实渲染核验这些方向。VisualPPTStudio 的双图校验、编辑式 clean prompt 反馈、固定页码工作流和人工可审核的多 Agent 结果是独立实现；本项目没有复制这些仓库的代码。细节与许可证核对见 [docs/OPEN_SOURCE_RESEARCH.md](docs/OPEN_SOURCE_RESEARCH.md)。

## 6. GitHub 对标与独立设计

这份项目由本次会话**独立编写代码**，没有直接复制他人的实现文件。阅读过的架构参考（建议实际使用前检查对应版本许可证和NOTICE）：

- [eddyzzl/imagegen-pptx-pipeline](https://github.com/eddyzzl/imagegen-pptx-pipeline)：把生成图当视觉目标、将原始内容当数据源、真实渲染QA、严格审计；本项目未复制其转换器。
- [LRriver/AIPPT](https://github.com/LRriver/AIPPT)：可编辑重建流程、VLM分析、显式回退策略及多模型路由；本项目按手动生图重新设计界面和上传流程。
- [seekskyworld/CreatPPT](https://github.com/seekskyworld/CreatPPT)：结构化场景描述、Web编辑器与PPTX导出解耦；本项目使用自主的归一化坐标JSON。
- [presenton/presenton](https://github.com/presenton/presenton)：通用主题、多模型支持、批量平台的成熟功能对标。
- [PptxGenJS](https://github.com/gitbrent/PptxGenJS)：以正常依赖方式使用，遵守其许可证。

不能保证所有第三方仓库当前主分支许可证或版本永远不变。本项目建议不复制仓库代码，而是参考架构和协议，自主实现独立功能。

## 7. 故障排查

**页面无法启动**：运行 `python scripts/doctor.py` 检查 Python/Node。访问 `/docs` 判断后端是否启动。首次依赖安装需要联网。

**导出失败：Missing pptxgenjs**：`cd renderer && npm install`；然后回到项目根目录。

**PPTX 能导出但没有预览**：安装 LibreOffice 并保证 `libreoffice` 或 `soffice` 命令在 PATH 中。真实导出的PPTX依然可用。

**没有高保真设计效果**：检查 `S001_clean.png` 是否存在且确实无文字；不要将 `design` 当 `clean` 上传，否则会出现重影。

**标题变了但PPT中不变**：页面规划字段 `title/body` 是文本事实源。请在内容规划修改并保存，或修改编辑器中ID为`title/body`的元素并点击保存布局。

**PPT字体不对**：PowerPoint/WPS/LibreOffice 依赖本机字体。将全局字体设为你的电脑存在的中文字体，必要时在原生元素里单独调整。

**错误提示图片找不到**：批量上传严格匹配 `S001_design.png`、`S001_clean.png`、`S001_asset_01.png`，不要把文件命名成 `第一张.png`；或者使用单页映射上传自动改名。

## 8. 安全边界

这是**仅供本机/可信环境**运行的开发原型，无账号系统、租户隔离、反向代理防护和生产级任务锁，不可直接绑定 `0.0.0.0` 暴露公网。项目资源在本地 `workspace/`，请自行备份。不要上传敏感文件到公开服务；AI调用会向外部服务发送用户选定内容。图像文件使用 PIL 解码校验，命名和项目路径有限制，但不代表已完成完整生产级安全审计。

**建议下一阶段**：在这版基础上增加蒙版可视化细化、背景原图/底图对齐校验、图片资产自动区域分离、文本框自动测量/断行、可编辑图表与复杂页面类型、页面级缓存并发、跨主题多套测试基准。


## v0.1 → v0.2 升级

可以直接解压 v0.2 使用。原来 v0.1 的 `workspace/` 是本地运行时数据，**压缩包中不包含**；如果要迁移已有项目，在退出旧程序后备份旧版 `visual_ppt_studio/workspace/`，复制到新版同名目录。保留旧目录备份以便回滚；不要把 `.env`、私钥或项目资料上传公共仓库。

**验证：**本版原来的测试套件 7 项 + 新增重建流程测试 4 项通过。快速运行 `python -m pytest -q`（需要先装 `requirements-dev.txt`），可通过 `/api/health` 检查可选引擎。针对用户电脑的真实 OCR、LaMa 安装和 Web 手势操作仍需要你本地验证。
