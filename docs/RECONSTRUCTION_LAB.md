# v0.2 图像重建实验室使用与技术说明

> 目标是 **设计稿与已知文案驱动的混合重建**，并非承诺任意图像的每个像素都能恢复成原生可编辑 PowerPoint 对象。

## 推荐流程（双图 + DeepSeek 监督 + 本地 OCR）

1. 在 01 规划并锁定页码，02 可用 DeepSeek 生成完整设计图 Prompt 与 Image 编辑模式专用的去字 Prompt；当前不调用 Image API，你手动在 ChatGPT Image 先生成完整设计图，再基于原图编辑出无字图。
2. 在 03 将同页完整图上传为 `S001_design.png`，无字图上传为 `S001_clean.png`。clean 上传后会绑定当前 design 的文件哈希；换了 design 就必须重新上传 clean。
3. 上传同页 design 与 clean 后会自动触发“DeepSeek 检查原图 / 无字图配对”，也可以手动重查。审核残字、布局漂移和非文字损伤。低分或存在非文字变化时不会推荐通过；可以按报告生成下一轮 Image 编辑 Prompt。人工仍需放大检查。
4. 进入重建实验室，在含字 design 上跑本地 OCR；DeepSeek 结构分析可作为另一组候选。没有 OCR 时仍可手工添加文字框。
5. **人工检查 OCR 结果**，确认中文/英文、分行、区域及 role。
   - `title` / `body`：只使用 OCR 位置，保留项目中已规划的原文；
   - `custom`：你必须人工核对该文字，确认后才进入 PPT；
   - `ignore`：被过滤，不修补也不导出。
6. 直接用上传的 clean 作 PPT 视觉底图；勾选需要可编辑的文字，提升为原生对象。只有 clean 缺失或局部质量不合格时，才尝试 `ink` / `rectangle` Mask + Telea/NS/可选 LaMa 的候选修补流程。
7. 按需从原图拖拽裁剪 `S001_asset_01.png` 等独立素材。普通裁剪不会补回遮挡像素。
8. 点击“提升勾选文字为原生对象”，进入 04 调整元素和字号，使用 design 对位叠加后保存布局。
9. 在 05 导出并由 LibreOffice 实际渲染。默认在导出后由 DeepSeek 对照原图、clean 和真实渲染结果监督当前选中页并提出校准建议；可关闭自动监督。应用后要保存并重渲染复核。

DeepSeek 的结构结果不会直接改项目。结果中每个对象有归一化坐标、类别、文本候选和置信度；OpenCV 会补充低置信度的矩形和长线候选。用户勾选后，文字进入 OCR 校对列表，矩形/椭圆/线条进入原生对象编辑器，插画区域则先裁成独立图片素材。文字内容、对象边界和图形类别都需要人工检查。插画裁剪保留原图可见像素，不等于透明分割；本版也不会把复杂插画或公式自动矢量化。

## 本地安装

主程序运行不需要 OCR 或 LaMa，保持 v0.1 的所有功能。

建议 macOS 先安装 **Python 3.11、Node.js 20+、LibreOffice**。

```bash
bash start_mac_linux.sh
```

打开 http://127.0.0.1:8765。可以手动执行扩展安装：

```bash
source .venv/bin/activate
python -m pip install -r requirements-reconstruction.txt
```

RapidOCR 安装后第一次调用需要下载所需模型文件（具体依赖包/版本行为为准），不是零网络环境必然可运行。Tesseract 是备选：

```bash
brew install tesseract tesseract-lang
python -m pip install pytesseract
```

LaMa 是可选的重型扩展，会引入 PyTorch，Mac 兼容性需要以当前版本及设备验证为准：

```bash
python -m pip install simple-lama-inpainting
```

如果 LaMa 未安装，系统会明确报错，不会默默改用另一种算法。

## 独立调试端点

所有 URL 中的 `pid` 是项目实际 ID，`sid` 为 `S001` 等编号。

| 操作 | 方法 / 路径 | 修改正式项目？ |
|---|---|---|
| 可用引擎、实验状态 | `GET /api/projects/{pid}/slides/{sid}/reconstruction` | 否 |
| OCR 文字识别 | `POST .../ocr?backend=auto` | 只写实验文件 |
| DeepSeek 视觉结构建议 | `POST .../analyze-structure` | 否，只返回草案 |
| DeepSeek 原图/clean配对审核 | `POST .../review-image-pair` | 否，只返回审核建议 |
| DeepSeek 双阶段 Image Prompt | `POST .../prompts/ai` | 否，只返回 Prompt |
| DeepSeek 文字校准建议 | `POST .../calibrate-text` | 否，只返回建议；人工应用后再保存 |
| 人工校正 OCR JSON | `PUT .../regions` | 只写实验文件 |
| Mask + 候选修复 | `POST .../clean-candidate` | 只写实验文件 |
| 查看候选/Mask/热力图 | `GET /lab/{pid}/{sid}/{filename}` | 否 |
| 审核升为 clean | `POST .../accept-clean` | **是，需显式确认** |
| 提取独立裁剪素材 | `POST .../extract-asset` | **是，新加 asset_XX** |
| 比较设计与实际导出图 | `POST .../compare-render` | 只写实验文件 |

`...` 表示 `/api/projects/{pid}/slides/{sid}`，并非直接复制的合法 URL。

### JSON 请求示例

```json
{
  "regions": [
    {"text":"人工已核对标题", "role":"title", "box":[0.08, 0.12, 0.60, 0.10]},
    {"text":"正文文本", "role":"body", "box":[0.08, 0.35, 0.50, 0.30]}
  ],
  "mask_mode":"rectangle",
  "method":"telea",
  "padding":3,
  "threshold":32
}
```

## 数据保护与工作原理

- `images/S001_design.png` 是不可变的参考目标，人工重新上传设计图会更改哈希；过期 OCR 和清洁候选须重新审核。
- `build/reconstruction/S001_ocr.json` 保存识别区域及 SHA256；`S001_mask.png` 保存真实修补范围；`S001_candidate.png` 保存候选；`S001_candidate_report.json` 保存参数。
- 在最终背景之外的像素，OpenCV 修补器不会主动变更它们；但要注意格式写入及分辨率转换带来的色彩差别。
- `ink` mask 使用边框颜色估计 + LAB 色彩差异，小字和渐变可能漏掉字符；`rectangle` 会破坏整个框内的背景但更彻底去除文字。
- LaMa 仍然推测隐藏纹理，不是恢复设计稿实际的隐形像素。
- 简单裁剪会保留原图中的文字和背景；不是 SAM 语义分割。透明化只支持单色背景的自动估计，不是万能抠图。
- `target`（设计稿）与 `rendered`（实际 PPT 导出）之间的像素差反映差异区域，但字体渲染、色彩管理、缩放都影响数值。不承诺“MAE低于N”就代表审美合格。
- PPT 里的中文文本要在用户的 PowerPoint 设备上安装合适字体，文本框字号仍需人工检查。

## 暂未实现

- 全自动逐像素还原复杂页面、SAM2自动图形分层、弯曲艺术文字的原生编辑、对任意图片的完美智能去字；
- 原生可编辑图表的自动数据恢复（需要有源数据或人工校对）；
- 无人值守分布式100套队列、权限系统、浏览器全自动集成测试。

下一步最有价值的是用 3 类 ChatGPT Image 真实样张（白底文字、渐变封面、密集图表）逐项评测、标注失败模式，然后再考虑 SAM 2 组件及更高精度字体测量。

## 参考项目（研究设计，未复制源代码）

- https://github.com/JadeLiu-tech/px-image2pptx ：OCR → 前景 Mask → LaMa → 原生文字，参考其模块职责和失败类型。
- https://github.com/LRriver/AIPPT ：参考文字识别、前景规划、背景与候选修补的中间产物。
- https://github.com/eddyzzl/imagegen-pptx-pipeline ：参考声明式场景、对象核验、实际渲染反馈。
- https://github.com/seekskyworld/CreatPPT ：参考 Deck/SlideSpec 数据结构及编辑与导出解耦。
- https://github.com/Sanster/IOPaint 、https://github.com/advimman/lama 、https://github.com/PaddlePaddle/PaddleOCR 、https://github.com/facebookresearch/sam2 ：后续扩展方向。

以上遵循仓库当时公开的说明与许可。若以后直接引入代码，请保留依赖许可证和归属说明，重新核查实际使用版本。
