# 本地单臂与双臂操作说明

本文用于操作员直接运行原子抓放任务。入口为 `python -m task_skills.run_task`，支持已实现的中文指令模板。外部上游系统使用 HTTP 网关；本地任务不需要 HTTP 网关、`launch_nodes.py` 或 ZMQ 服务。

## 1. 环境与现场准备

所有命令在当前仓库根目录执行。真机使用 Windows 和已配置的机器人 Python 环境；已有 Conda 环境可用 `conda activate dobot-xtrainer` 激活。预览只编译图并保存文件，不连接硬件。

| 绑定 | 操作对象 | 放置目标 |
| --- | --- | --- |
| 物理左 Nova | A | target_A |
| 物理右 Nova | B | target_B |

真机前确认：控制网络和串口可用，示教配置与装置一致，物体位于初始位置，目标无占用，当前无持物或未完成动作，急停可用。停止正在运行的 HTTP 网关、主手遥操作及其他控制进程。

所有控制入口和代码副本共用同一个设备锁路径。在使用的每个终端中设置：

```powershell
$env:DOBOT_DEVICE_LOCK_PATH = 'C:/DobotRuntime/device-owner'
```

本地任务编译后的启动节点包含等待控制器、清报警、使能及配置速度，默认速度与加速度限制为 10%。必须先处理现场异常，不能用重复执行任务代替故障处置。本装置夹爪约定为 0 闭合、255 张开；重新安装或移植时须实测方向及物理通道。

## 2. 单臂操作

### 2.1 左臂：A 放至目标 A

先预览，正常编译为 15 个左臂节点：

```powershell
python -X utf8 -B -m task_skills.run_task `
  "把物体A抓取放到目标A" --dry-run
```

现场条件满足后执行：

```powershell
python -X utf8 -B -m task_skills.run_task `
  "把物体A抓取放到目标A"
```

### 2.2 右臂：B 放至目标 B

先预览，正常编译为 15 个右臂节点：

```powershell
python -X utf8 -B -m task_skills.run_task `
  "把物体B抓取放到目标B" --dry-run
```

现场条件满足后执行：

```powershell
python -X utf8 -B -m task_skills.run_task `
  "把物体B抓取放到目标B"
```

单臂任务只向参与臂下发指令，任务末尾将该臂返回示教 home；另一条臂不归位、不运动。当前单臂图没有末尾 DisableRobot 节点，不能将任务结束理解为双臂都已禁用。

## 3. 双臂顺序抓放

此指令先完成 A 抓放和左臂归位，再执行 B 抓放和右臂归位。它是 30 个节点的顺序图，不是双臂并行操作。

```powershell
$task = '把物体A抓取放到目标A，然后把物体B抓取放到目标B'

# 预览
python -X utf8 -B -m task_skills.run_task "$task" --dry-run

# 现场条件满足后执行
python -X utf8 -B -m task_skills.run_task "$task"
```

该顺序图末尾同样没有 DisableRobot 节点。运行前 A/B 必须位于初始位置；如果刚完成单臂演示，需要先恢复对应物体位置。

## 4. 双臂协调保持与返程

此模板编译为 43 个节点：双臂初始化并归位后并行抓取；左臂保持 A 5 秒，右臂放置 B；左臂放置 A 后，右臂等待两次放置均成功，再抓回 B 并放回初始位置。最后两臂归位并分别禁用。

以下指令须完整复制，不能任意缩写或更换保持时间：

```powershell
$task = '用左臂把物体A拿起来，然后右臂把物体B拿起来。' +
        '左臂拿起物体A以后保持5秒，右臂直接送到目标B。' +
        '然后5s后左臂再把物体A送到目标A。' +
        '随后右臂把物体B从目标B拿起来放回物体B原来的位置。'

# 预览
python -X utf8 -B -m task_skills.run_task "$task" --dry-run

# 现场条件满足后执行
python -X utf8 -B -m task_skills.run_task "$task"
```

当前自然语言解析器是有限模板，不是通用语言模型。该完整长句匹配已实现的协调任务；例如单独写“从目标B抓回物体B并放回原位”不保证可解析。

本地协调执行器和 HTTP 网关 live 模式都允许依赖图中独立的跨臂技能并行；同臂动作仍按依赖顺序执行。依赖图不替代实时碰撞检测；并行任务需按当前双臂装置和示教场景核验。

## 5. 真机确认、记录与后续切换

去掉 `--dry-run` 后，程序先打印完整执行图、解析后位姿和速度限制，再提示：

```text
输入大写 YES 执行真机任务:
```

只有精确输入 `YES` 才进入设备锁与硬件连接流程；其他输入取消，退出码为 2。命令中没有绕过该确认的参数。

每次运行在 `task_skills/artifacts/run_.../` 保存执行图、事件和摘要，末尾打印 `artifacts:` 路径。预览也生成独立记录；摘要中的 `hardware_connected=false` 说明该预览没有连接机械臂。可用 `--artifacts` 指定输出根目录，或用 `--scene` 指定经过确认的场景文件。

成功执行后，控制器收敛和夹爪反馈属于 `command_trace`；物体抓稳、放准仍需现场或独立传感器确认。失败后保留图和事件，先确认设备静止及物体状态，不直接重放任务。

本地真机任务会标记共享场景失效。后续切换至 HTTP live 网关时，需要关闭控制进程、现场恢复初始场景并记录网关 operator 复位；仅释放设备锁并不恢复逻辑场景。操作流程见[执行网关说明](execution-api/README.md)。
