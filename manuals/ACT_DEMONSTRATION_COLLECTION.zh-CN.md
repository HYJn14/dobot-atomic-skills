# ACT 拧瓶盖示教数据采集操作手册

更新：2026-11-06。适用于 `E:\Dobot\dobot-atomic-skills` 当前 Windows 主从遥操。

目标：从统一空闲姿态出发，实体左臂抓住并固定瓶身，实体右臂拧盖，直到瓶盖完全脱离。使用当前夹爪、顶部单相机；本机采集、检查与转换，训练和推理在其他机器完成。

## 1. 首次使用前

1. 使用无防拆环的空试剂瓶，选定一种瓶型、盖子和统一初始松紧程度。先验证人工遥操能够重复完成，再正式采集。
2. 确认两臂空闲准备姿态、瓶子放置区域、取盖后的结束位置。启动前移走夹爪开闭范围内的瓶子：服务 A 会执行原有夹爪开闭初始化。
3. 实机标准：**RIGHT 右从臂 .1 / 右夹爪 GRIPPER_RIGHT / COM4 / ID22；LEFT 左从臂 .2 / 左夹爪 GRIPPER_LEFT / COM3 / ID21**。14 维数据继续按 **`.1 六轴、.1 夹爪、.2 六轴、.2 夹爪`** 保存，即右侧在前、左侧在后。HAND_LEFT/RIGHT 是主手配置段，按原标定路由保留。
4. 现场已确认夹爪 **0=完全闭合、255=完全打开**。保存量为驱动位置/255，因此训练观测和动作均为 **0=闭合、1=打开**；数值方向不改变。分别空载开闭两个夹爪验证实际反馈。
5. 检查顶视画面。现已移除旧裁切和拉伸，预览、显示与录制均使用完整 640×480 画面。保留现场确认正确的默认180°旋转。瓶口、两只夹爪和取盖过程应在画面内。新机位/方向/画幅/夹爪绑定另建数据版本，建议本次用 `bottle_cap_layout_v2`。

6. 保持现有标定、工作区和限速设置，按主操作手册完成启动前检查。完整控制细节与锁恢复见 [原遥操手册](../TELEOP_WINDOWS_SETUP.zh-CN.md) 第 9～11 节。

相机独立预览（先退出占相机的旧 B，不需要启动 A）：

```powershell
Set-Location E:\Dobot\dobot-atomic-skills
E:\Dobot\.venv\Scripts\python.exe -X utf8 -B -m scripts.5_camera_read
```

Q/Esc/Ctrl+C 退出。该入口只开相机，预览方向与采集相同。旧 JPG/MP4 裁掉的边缘没有保存，修改后重新录制才能得到全画幅。

采集工具所需的额外依赖只有 h5py，本机已安装。重建环境时运行：

```powershell
Set-Location E:\Dobot\dobot-atomic-skills
E:\Dobot\.venv\Scripts\python.exe -m pip install -r requirements-act-data.txt
```

## 2. 退出旧程序后启动新版

本次实机绑定修正需要停止录制与跟随、退出旧 B 和 A，核验并归档旧设备锁后按先 A 后 B 完整重启。A 将 .1 连接至 COM4/ID22、.2 连接至 COM3/ID21。首次启用 ACT 时两终端都设置反馈开关。
终端 A：

```powershell
Set-Location E:\Dobot\dobot-atomic-skills
$env:DOBOT_DEVICE_LOCK_PATH = 'E:/Dobot/.local/runtime/device-owner'
$env:DOBOT_ACT_RECORDING = '1'
E:\Dobot\.venv\Scripts\python.exe -X utf8 -B experiments\launch_nodes.py --hostname 127.0.0.1 --robot-port 6001
```

该命令会使能控制柜，设置原有工具和速度参数，并依次初始化两侧夹爪。等到 `Starting robot server on port 6001`。遇到设备锁占用，按原手册 11.2 确认所有者后处理，不能换锁路径启动。

终端 B：

```powershell
Set-Location E:\Dobot\dobot-atomic-skills
$env:DOBOT_DEVICE_LOCK_PATH = 'E:/Dobot/.local/runtime/device-owner'
$env:DOBOT_ACT_RECORDING = '1'
Remove-Item Env:DOBOT_ALLOW_SAFE_POSE_INIT -ErrorAction SilentlyContinue
E:\Dobot\.venv\Scripts\python.exe -X utf8 -B experiments\run_control.py --hostname 127.0.0.1 --robot-port 6001 --show-img --project-name bottle_cap_layout_v2
```

两个终端都要设置 `DOBOT_ACT_RECORDING=1`。B 检测不到 A 的真实反馈字段会报错，退出后按顺序重启 A、B；不要在旧 A 上继续采。

观察 B 的相机、映射与跟随初始化信息，确认录制根目录为：

`E:\Dobot\datasets\bottle_cap_layout_v2\collect_data`

初始姿态检查与自动归位沿用原逻辑。没有到 home 时按原手册人工核对，不为跳过检查打开自动归位开关。

## 3. 第一次先做空载录制验收

1. 不录制，按现有操作方法分别启停跟随，验证主从方向及夹爪控制。
2. A 键短按不足 0.5 秒切换主手锁定；长按超过 1 秒切换该侧跟随。两侧先完成慢速对齐，观察 `ALIGN_DONE`，再开始录制。0.5～1 秒区间可能无动作。
3. 两侧跟随都开启、对齐完成后，按任一 B 键并松开一次，观察 `RECORD_START` 和 episode 路径。两只 B 功能相同，共用一个录制状态：右 B 开始后，左 B 会停止；下一次任一 B 再开始新 episode。不是各自录各自的臂。以完整按下并松开为一次操作；同一轮检测到两只同时松开只切换一次，先后松开可能切换两次，因此每次只按一只 B。
4. 空载缓慢移动两臂，分别开闭两个夹爪，持续约 10 秒。
5. 再完整按下并松开 B，观察 `RECORD_STOP`。不要在停止录制前关闭跟随。
6. 等待文件稳定，确认该 episode 中出现 `episode.json`、`closed.json`、`observation` 和 `topImg`。
7. 按下一节检查，确认质量报告 PASS，并检查 `telemetry.gripper_range` 两个通道有明显变化。没有变化时先核对动作和反馈，不正式采集。
8. 将空载记录标为 failure，备注“空载验收”，使其不进入成功训练集。

录制开启了约 10 Hz 后台夹爪反馈读取，读写事务共用串口锁；观测只读取缓存。现场要比较开关 ACT 模式前后的跟随响应及 `CONTROL_LOOP_PERF`。出现新增明显卡顿或反馈持续过期时先停止采集并反馈日志。

## 4. 每条拧盖示教

1. 在录制外复位瓶子、盖子和双臂，检查松紧程度及摆放位置。
2. 让两臂进入统一准备姿态，两侧跟随均开启且已完成对齐。录制从抓取之前开始。
3. 按 B 开始，留一小段稳定起始画面。
4. 实体左臂抓瓶并保持稳定；实体右臂抓盖、旋转，必要时按统一方式松开换抓。不要把人工摆瓶、等待或复位混进示教。
5. 瓶盖脱离后稍微抬离瓶口，短暂停留以便判定完成，再按 B 停止。
6. 等待落盘完成，记录 episode 名称并进行检查、预览和标注。

失败也按 B 正常结束，标为 failure 并填写原因。机器人/相机/串口异常先按原手册处理；中断且没有完整收尾的记录会被检查器拒绝。不要手工创建 `closed.json` 或改时间数据强行通过。

先采 5 条试验数据，全部检查并交训练端试读，通过后再补至约 50 条成功记录。成功示教以稳定流程为主，可保留少量自然纠偏后成功的完整轨迹。失败另存，默认不进入第一版 ACT。

## 5. 检查、预览与标注

在终端 C 操作，替换下面的示例 episode 名。建议停止当前录制并等待落盘后执行，正式批量转换在退出 B 后执行。

```powershell
Set-Location E:\Dobot\dobot-atomic-skills
$episode = 'E:\Dobot\datasets\bottle_cap_layout_v2\collect_data\替换为实际episode名称'
E:\Dobot\.venv\Scripts\python.exe -X utf8 -B -m dobot_control.act_dataset check $episode
```

输出 `PASS` 表示技术检查通过，成功与否仍由任务结果标注决定。结果写入 episode 内的 `quality_report.json` 和同名 HTML。

检查器验证：schema 2、正常收尾、保存计数、14 维有限数值、夹爪范围、双臂/双夹爪使能、发送完成、单调时间、图像完整、图像帧号、反馈时效和指令间断。当前状态/图像最大年龄 0.2 秒、夹爪反馈最大年龄 0.35 秒、相邻指令最大间断 0.2 秒。超过阈值会报告具体文件和原因。

生成回看视频：

```powershell
E:\Dobot\.venv\Scripts\python.exe -X utf8 -B -m dobot_control.act_dataset preview $episode --output "$episode\preview.mp4"
Start-Process "$episode\preview.mp4"
```

视频采用时间戳重采样并叠加时间、样本编号；不会把原始高速重复行直接当 15 fps 播放。输出文件存在时拒绝覆盖，需要重生成时选择新名称。

成功标注示例：

```powershell
E:\Dobot\.venv\Scripts\python.exe -X utf8 -B -m dobot_control.act_dataset label $episode --status success --note '瓶型A，空瓶，无防拆环，统一手拧松紧；左固定右拧；换抓2次；盖子完全脱离；已核对.1/.2实体侧和夹爪开闭方向'
```

失败标注示例：

```powershell
E:\Dobot\.venv\Scripts\python.exe -X utf8 -B -m dobot_control.act_dataset label $episode --status failure --note '抓盖打滑，未完成；瓶型A'
```

标签默认 `unreviewed`。只有 `success` 且质量检查通过的记录才能导出。失败与未标注数据保留原处。标签可通过同一命令重新设置，备注应记录瓶型、初始松紧、成功结果、换抓/纠偏及现场通道核验。

## 6. 一次转换为 ACT 数据集

完成当天采集并退出 B 后：

```powershell
Set-Location E:\Dobot\dobot-atomic-skills
E:\Dobot\.venv\Scripts\python.exe -X utf8 -B -m dobot_control.act_dataset convert 'E:\Dobot\datasets\bottle_cap_layout_v2\collect_data' --output 'E:\Dobot\datasets\bottle_cap_layout_v2\act_v1' --fps 15
```

输出目录必须是新目录，避免新旧 episode 和划分混在一起。再次导出使用 `act_v2`。每条候选记录都会给出质量与标签结果；技术合格但 failure/unreviewed 的记录不导出。没有可导出记录时命令以失败退出。

输出：

```text
act_v1/
  episode_0.hdf5
  episode_1.hdf5
  ...
  dataset_manifest.json
  splits.json
  quality_report.json
  quality_report.html
```

50 条成功记录默认生成 40 训练/10 验证，固定随机种子 0，按整条 episode 划分。少量试采也能导出；只有一条时没有验证集，不用于正式训练评估。不同 INI 哈希、相机列表或通道顺序不能混成同一个数据版本。

转换按统一 15 Hz 网格，选择网格时刻之前最近的完整样本，状态、图像和动作来自同一行；不使用未来帧，不对夹爪切换插值。图像转 RGB，HDF5 使用无损 LZF 压缩。保存的动作是通过检查后的目标，不等同于每一时刻机器人已经到位。速度在转换阶段按重采样后的实际状态差分计算。

## 7. 交给训练人员的内容

交付 `act_v1` 整个目录、以下代码及本手册；原始数据另外保留备份：

- `dobot_control/act_dataset.py`
- `dobot_control/act_recording.py`
- `scripts/act_training_adapter.py`
- 对应的包目录/`__init__.py`，或直接交付这次源码版本。

HDF5：`observations/images/top [T,480,640,3] uint8 RGB`；`observations/qpos [T,14]`；`observations/qvel [T,14]`；`action [T,14]`；`timestamp [T]`；`source_index [T]`。

训练端必须采用本项目的同一行观测/动作配对，取消原 ALOHA 加载器的真实数据 `start_ts-1` 偏移。使用提供的适配器可处理变长 episode 与动作 padding，并只用训练集计算归一化统计。训练端在仓库根目录运行：

```text
python -m scripts.act_training_adapter /path/to/act_v1
```

该命令在已安装 PyTorch 的训练机器上读一个 batch；本机不运行。接入 ACT 时使用 `from scripts.act_training_adapter import load_data`，调用 `load_data(dataset_dir, batch_size=8, chunk_size=30)`，返回 train_loader、val_loader、stats、is_sim。单相机 `camera_names=['top']`，state/action 14 维，chunk_size 与模型 num_queries 一致。保留训练端 ACTPolicy 原有图像归一化，不重复预处理。

训练端还需用一个 batch 调用其 ACTPolicy 完成前向及反向检查，保存统计量、划分、相机与动作约定。本次本机验收覆盖 HDF5 和 NumPy 加载，不包含 ACT 模型运行。

## 8. 正常退出、常见问题与回退

先按 B 停录并等待文件稳定，再分别长按 A 关闭跟随，确认实体静止、主手支撑并锁定；B Ctrl+C 后检查写盘是否正常结束，再 A Ctrl+C。按原手册完成控制柜与设备锁处理。进程退出不会自动保证机器人禁用或夹爪松开。

| 现象 | 处理 |
|---|---|
| B 提示 A 没有 ACT 反馈 | 检查两个终端环境变量并重启 A、B |
| gripper stale/invalid | 停止采集，检查 A 串口、真实开闭反馈与日志；不要用命令填反馈 |
| 图像过期或帧号不增长 | 检查相机/USB/进程占用，修复后重采 |
| both arms/grippers must be enabled | 录制前两侧开启并等待对齐完成；单侧调试记录不作为完整任务 |
| 未正常收尾/计数不符 | 检查是否异常退出、写盘故障或队列溢出；保留原数据并重采 |
| 有 RECORD_QUEUE_FULL | 保存原始日志，检查磁盘与主机负载，质量报告判断时间间断 |
| 导出数量少于成功数量 | 看质量报告；成功标签不会覆盖技术错误 |
| 旧3段调试记录被拒绝 | 属正常行为；旧数据缺真实反馈与新时间字段，不能补造 |
| 反馈模式下出现新增跟随卡顿 | 停录、退出 B/A，在两个终端移除 ACT 环境变量并重新启动普通遥操；保留日志用于定位 |

普通遥操回退：两个终端执行 `Remove-Item Env:DOBOT_ACT_RECORDING -ErrorAction SilentlyContinue`，按原手册重启 A、B。普通记录不会被 ACT v2 转换器接收。

首次现场验收请记录：不录制与录制时跟随响应、夹爪通道对应、真实开度变化、B 第一次开始/第二次停止、质量报告、视频、一次完整拧盖结果。本轮开发未操作真实机器人，这些现场项目由操作者按本手册完成。

## 近期操作补充

- 夹爪端点单独检查：先退出 A/B，双击仓库根目录 `open_gripper_panel.cmd`；选择左 COM3/ID21 或右 COM4/ID22，再点全开/全闭。完成后关闭窗口，再按第 2 节重启 A/B。
- 本次新记录带 `hardware_layout_version=2` 和修正的 gripper_ports；转换器按绑定版本隔离数据。历史 4 段调试录制仍保持 failure 与原回看视频，正式采集使用新目录。
- 报警后出现 RECOVERY_LIGHT_FAILED 表示指示灯更新失败，程序仍执行遥操复位。查 DobotStudio 报警；GetErrorID 第一组 -2 为碰撞检测触发。处理触发原因后恢复。
- `Safe stop before pose motion` 的显式归位入口见主手册 9.3，home 为右 .1 `[90,0,90,0,-90,-90]`、左 .2 `[-90,0,-90,0,90,90]`。
