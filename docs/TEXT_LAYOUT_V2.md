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

本机未安装 Microsoft PowerPoint。所有自动图片明确标记为 LibreOffice
辅助预览；Office 验收未完成，不能由分数或预览替代。

## 实施约束

Python 统一布局在候选生成或显式编辑时发生；保存后的布局直接用于预览和导出。
TextBlock → TextLine → TextRun 保留原始文字、原始框、字符范围、稳定 ID、
空格归属、字体文件及度量、baseline、最终 box 和冲突。每个 run 为独立单行对象。
导出不缩字、不重新排版。旧数据先返回可审查的迁移候选并保留原始快照。
缺失字体只能使用明确记录的回退，不能把代理字体当成原字体测量。
