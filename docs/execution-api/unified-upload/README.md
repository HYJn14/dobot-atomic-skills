# 3.2 → 3.3 统一执行结果上传联调

这个目录保存“具身操作已经完成后”的模拟执行记录、字段设计草案和上传联调样例。它使用外部统一上传信封：

- `schema_version`: `dobot.execution.v1`
- `POST /api/v1/robot-executions`
- 默认目标：`http://100.79.134.36:8000`
- 请求类型：`application/json`

目录中的 JSON 是测试 fixture，不会自动触发机器人动作。`scripts/upload_robot_execution.py` 只负责读取已经生成的执行记录并上传。

## 文件

- `3.2真机到3.3_统一字段设计表_v1.0草案.md`：字段与响应约定。
- `fixtures/robot_execution_succeeded.json`：模拟执行成功记录，47 条事件、40 条原子日志。
- `fixtures/robot_execution_failed.json`：模拟执行失败记录，32 条事件、28 条原子日志。
- `scripts/upload_robot_execution.py`：校验、试运行和 POST 上传脚本。
- `tests/test_unified_upload_fixtures.py`：不访问网络的 fixture 契约测试。

## 指令

在项目根目录执行：

```powershell
cd C:\Users\49459\Documents\Codex\projects\dobot-atomic-skills

# 只校验成功样例，不访问网络
python .\scripts\upload_robot_execution.py --fixture success --validate-only

# 只校验失败样例，不访问网络
python .\scripts\upload_robot_execution.py --fixture failure --validate-only

# 运行本地契约测试
python -m pytest .\tests\test_unified_upload_fixtures.py -q

# 实际上传成功样例
python .\scripts\upload_robot_execution.py --fixture success

# 实际上传失败样例
python .\scripts\upload_robot_execution.py --fixture failure
```

也可以通过环境变量覆盖目标地址：

```powershell
$env:ROBOT_EXECUTION_BASE_URL = "http://100.79.134.36:8000"
python .\scripts\upload_robot_execution.py --fixture success
```

若服务需要 Bearer Token，把 Token 放在环境变量 `ROBOT_EXECUTION_BEARER_TOKEN` 中；不要把真实 Token 写入脚本、fixture 或 Git。配置 Token 时脚本只允许使用 `https://` 地址，会拒绝通过当前明文 HTTP 地址发送 Token。

上传脚本会打印 HTTP 状态码和服务响应，并校验回执中的 `receipt_id`、原始 `message_id`、`execution_id`、`stored`、`duplicate`、`content_sha256` 和 `warnings`。只有 HTTP `2xx` 且回执完整匹配才会以成功退出；`4xx/5xx` 会保留服务返回体并以失败退出。超时、断连和 `5xx` 最多自动重试两次，`4xx` 不自动重试。
