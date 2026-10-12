# Image-to-editable-PPT 开源调研

调研日期：2026-10-10。只参考公开项目描述与许可证；没有复制第三方源代码、提示词或素材。项目采用自己的实现、数据结构和界面。

| 项目 | 公开方案 | 对 VisualPPTStudio 有用的经验 | 许可证观察 |
|---|---|---|---|
| [Image2PPT](https://github.com/dlinjiade-debug/Image2PPT) | RapidOCR 识别文本，修复文字区域，按文字样式聚类，再把剩余内容切成可移动图片模块 | 文本框聚合要保守；复杂内容先作为独立图片，不要假装全都能矢量化 | 仓库 README 标注 MIT；RapidOCR 另有自己的 Apache-2.0 许可 |
| [px-image2pptx](https://github.com/JadeLiu-tech/px-image2pptx) | PaddleOCR、OCR 框与字符 mask 交集、LaMa 背景修复、原生文本框组装 | OCR 框不能单独决定擦除区域；用字符级 mask 限制副作用，并保留每阶段中间结果方便调试 | 仓库标注 MIT；模型权重和模型依赖需分别遵守其许可证 |
| [ppt-screenshot-to-editable-pptx](https://github.com/NightSensingLab/ppt-screenshot-to-editable-pptx) | 原生文字/简单形状与裁剪图像混合，真实渲染并验证 PPTX | 复杂照片、图表主体可以保留图像；必须同时证明文本对象结构和视觉渲染 | 仓库标注 MIT |
| [sort-tech/image-to-ppt](https://github.com/sort-tech/image-to-ppt) | Vision 模型输出受 schema 约束的 SlideSpec，再验证和构建原生形状、连接线、表格及文本 | 结构化中间表示和“防止整页扁平图片”的硬性 QA 很值得借鉴 | 调研时 README 页面没有看到许可证声明；本项目仅参考架构描述，不复用代码 |

## 本项目采用的差异化流程

本项目当前按用户确定的路线实现：

```text
用户手动上传 design + Image 编辑生成的 clean
  → DeepSeek 检查配对差异
  → OCR 识别文字并由用户校对
  → 原生 PPT 文字对象
  → LibreOffice 真实渲染
  → DeepSeek 监督当前页并提出文字校准建议
```

配对审核出现问题时，用户可让 DeepSeek 根据当前两张图和审核证据，生成下一轮 Image 编辑提示词，再手动生成、上传并复查。Image API 尚未接入。DeepSeek 的修订建议不会自动改写原图或 PPT 对象，避免没有用户确认就覆盖内容。

不同于以单张图 OCR 后本地擦字为主的工具，本项目把用户生成的 clean 图作为主要底图，将本地 OpenCV/LaMa 修复保留为备用实验流程。当前质检还是多项独立 Agent 功能，不声称全自动像素级还原；密集信息图中的小文字仍需检查可编辑覆盖率和原生文本是否完整。
