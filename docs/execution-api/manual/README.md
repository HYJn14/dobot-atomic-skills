# 手册生成与维护

当前接口说明以 [Markdown 源稿](../Dobot_原子技能执行接口与联调手册_v1.1.md) 为准；[PDF v1.1](../Dobot_原子技能执行接口与联调手册_v1.1.pdf) 尚未包含本次接口更新，待后续重新构建。生成器读取正文并构建目录、书签、表格、代码与矢量依赖图。文档构建不依赖机器人环境，也不连接设备。

v1.1 正文只覆盖 `embodied.pick/place`。第二个技能族 `dobot.*`（化学原子，见 [../README.md](../README.md) 的「化学原子技能组」一节）尚未并入正文；并入时应同时更新封面、版本号、修订说明与依赖图，并重新渲染 PDF 核对，而不是只改 Markdown。

## 构建

Python 3.10 或更新版本，在独立文档环境安装 `requirements.txt`。准备本地 Noto Sans SC 可变字体和支持 ASCII 的等宽字体（例如 DejaVu Sans Mono）。从本目录执行：

```powershell
python -m pip install -r requirements.txt
python build_manual.py `
  --source ../Dobot_原子技能执行接口与联调手册_v1.1.md `
  --output ../Dobot_原子技能执行接口与联调手册_v1.1.pdf `
  --cjk-font 'C:/Fonts/NotoSansSC-VF.ttf' `
  --mono-font 'C:/Fonts/DejaVuSansMono.ttf'
```

Linux 使用同一组参数，通过反斜杠续行或单行调用。字体缓存位于输出目录的 `.font-cache/`，按输入字体哈希和字重区分，不纳入 Git。正文和标题分别使用 400、600 字重；代码使用等宽字体。

字体路径为示例，应替换为实际文件。代码块和反引号内容使用 ASCII；中文解释放在正文，避免等宽字体缺字。

## 校验

- 单臂和 5 秒双臂示例位于 `../examples/`。运行包可省略 `catalog_revision`；如提供，该字段是上游元数据，不需与能力目录中的值一致。联调时仍可读取目录查看设备、技能和执行配置，再生成任务及幂等键。
- 发布前对示例执行 Schema 与当前 `Catalog.validate` 校验，核对命令参数和响应字段。
- 渲染并逐页检查 PDF；检查缺字、代码宽度、表格换行、孤立段落、页码与目录。
- 检查正文和 PDF 元数据，确保没有个人称呼、个人设备路径或会话记录。

文档版本、封面、页眉、修订说明和文件链接应同步维护。接口实现版本应单独标识，文档排版修订不表示机器人功能发生变化。
