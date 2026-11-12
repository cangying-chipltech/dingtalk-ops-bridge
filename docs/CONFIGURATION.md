# 配置说明

本文档说明环境变量、动作、监控和状态文件。首次使用建议先看根目录 `README.md` 的快速演示。

## 配置文件位置

| 文件 | 用途 | 容器路径 |
| --- | --- | --- |
| `.env` | 钉钉凭据、Webhook 和运行参数 | Compose 环境变量 |
| `config/actions.json` | 可执行动作与批处理 | `/app/config/actions.json` |
| `plugins/nodes/config/nodes.json` | 节点插件登录信息和认证方式 | `/app/plugins/nodes/config/nodes.json` |
| `plugins/swe-runner/config/swe-runner.json` | swe-runner 插件目标配置 | `/app/plugins/swe-runner/config/swe-runner.json` |
| `config/monitors.json` | 周期监控项 | `/app/config/monitors.json` |
| `data/monitor-state.json` | 监控状态，自动生成 | `/app/data/monitor-state.json` |

开发或测试时可以用 `CONFIG_DIR`、`ACTIONS_FILE`、`MONITORS_FILE`、`MONITOR_STATE_FILE` 和 `ACTION_PLUGIN_DIR` 覆盖默认路径。插件私有环境变量由插件文档定义。

## 环境变量

### 钉钉连接

必须配置以下两种方式之一：

```text
DWS_UNIFIED_APP_ID=<unified app id>
```

或者：

```text
ROBOT_CLIENT_ID=<robot app key>
ROBOT_CLIENT_SECRET=<robot app secret>
```

可选参数：

| 变量 | 说明 |
| --- | --- |
| `DWS_ALLOWED_USERS` | 允许使用机器人的用户 |
| `DWS_ALLOWED_GROUPS` | 允许使用机器人的群 |
| `DWS_NOTIFY_STAFF_ID` | `dws` 通知人员 |
| `DWS_EXTRA_ARGS` | 追加给 `dws dev connect` 的参数 |

### Webhook

| 变量 | 说明 |
| --- | --- |
| `DINGTALK_WEBHOOK_URL` | 钉钉自定义机器人 Webhook，完整 URL |
| `DINGTALK_WEBHOOK_SECRET` | 加签密钥，可选 |
| `DINGTALK_WEBHOOK_KEYWORD` | 自定义关键词，可选 |
| `DINGTALK_ALERT_AT_MOBILES` | 告警时 @ 的手机号，逗号分隔 |
| `DINGTALK_WEBHOOK_TIMEOUT_SECONDS` | 请求超时，默认 10 秒 |

项目会按钉钉规则对 `timestamp + secret` 做 HMAC-SHA256 加签。关键词已存在时不会重复插入。

### 运行参数

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Python 日志级别 |
| `ACTION_MAX_CONCURRENCY` | `8` | 单次命令最多并发动作数 |
| `ACTION_MAX_OUTPUT_CHARS` | `3600` | 单个动作保留的输出字符数 |
| `COMMAND_MAX_REPLY_CHARS` | `12000` | 单条钉钉回复最大字符数 |
| `ACTION_PLUGIN_DIR` | `/app/plugins` | 外部 action 仓库挂载目录 |
| `MONITOR_MAX_CONCURRENCY` | `8` | 单轮监控最大并发数 |
| `MONITOR_DEFAULT_INTERVAL_SECONDS` | `60` | 未指定时的默认检查间隔 |
| `MONITOR_MAX_OUTPUT_CHARS` | `4000` | 命令监控保留的输出字符数 |

## 钉钉命令

| 指令 | 说明 |
| --- | --- |
| `help` | 显示命令菜单 |
| `list` | 列出动作和批处理 |
| `nodes` | nodes 插件：列出节点清单，不显示密码 |
| `run disk,memory,uptime` | 并发执行多个动作 |
| `run ping host=10.0.0.12` | 执行带参数的动作 |
| `run node-login node=web-01` | 验证节点 SSH 登录 |
| `run node-info node=web-01` | 查看节点主机名、运行时间和用户 |
| `run node-disk node=web-01` | 查看节点磁盘 |
| `batch node-health node=web-01` | 并发执行节点登录、信息和磁盘检查 |
| `batch health` | 并发执行预置动作组 |
| `monitor list` | 列出监控项 |
| `monitor check api-health,redis-port` | 立即执行指定检查 |
| `status` / `history` | swe-runner 插件默认目标查询（插件未加载时 `status` 查询监控） |
| `<目标或别名> status|history` | swe-runner 插件指定目标查询 |
| `check` | 立即执行全部已启用检查；始终由框架处理 |

命令支持 `ops` 前缀。加载 swe-runner 插件且配置了默认目标时，裸 `status` 优先交给插件；否则回退为即时监控检查。裸 `check` 不会被插件接管，旧语义请用 `swe-runner check`、`query` 或 `<目标> check`。

## 节点登录配置

节点清单由 `plugins/nodes/` 插件管理，默认读取
`plugins/nodes/config/nodes.json`。可以参考
`plugins/nodes/config/nodes.example.json`：

```json
{
  "nodes": {
    "web-01": {
      "description": "Web 节点",
      "host": "10.0.0.11",
      "port": 22,
      "user": "ops",
      "passwordEnv": "NODE_WEB01_SSH_PASSWORD",
      "strictHostKeyChecking": "accept-new",
      "connectTimeoutSeconds": 10
    },
    "worker-01": {
      "description": "使用 SSH 私钥的节点",
      "host": "10.0.0.12",
      "user": "ops",
      "identityFile": "../../../data/ssh/id_ed25519_worker",
      "knownHostsFile": "../../../data/known_hosts",
      "strictHostKeyChecking": "yes"
    }
  }
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `host` | 必填，节点地址 |
| `user` | 必填，SSH 用户 |
| `port` | 默认 22 |
| `passwordEnv` | 密码所在环境变量名，不能直接写 `password` |
| `identityFile` | SSH 私钥路径，相对路径以节点配置文件所在目录为基准 |
| `knownHostsFile` | known_hosts 文件，默认 `DATA_DIR/known_hosts`（容器内为 `/app/data/known_hosts`） |
| `strictHostKeyChecking` | `accept-new`、`yes` 或 `no`，默认 `accept-new` |
| `connectTimeoutSeconds` | SSH 连接超时，默认 10 秒 |

密码模式使用 `sshpass -e`，密码通过 `SSHPASS` 环境变量传入，不会出现在命令行参数或动作反馈中。`nodes` 命令也不会显示密码或环境变量值。插件私有变量 `NODES_CONFIG` 可覆盖配置文件路径，旧 `NODES_FILE` 仍兼容。

私钥建议放在 `data/ssh/`，该目录不会被打进镜像：

```bash
mkdir -p data/ssh
chmod 700 data/ssh
# 将私钥放到 data/ssh/id_ed25519_worker
chmod 600 data/ssh/id_ed25519_worker
```

节点动作由 `plugins/nodes/config/actions.json` 随插件提供，通过 `"type": "node"` 和
`remoteCommand` 定义。默认提供：

- `node-login`：执行 `hostname`，验证登录。
- `node-info`：执行 `hostname && uptime && whoami`。
- `node-disk`：执行 `df -hP`。

## swe-runner 插件配置

swe-runner 的配置、命令兼容层和动作类型都位于 `plugins/swe-runner/`，不属于框架配置。
目标文件默认为 `plugins/swe-runner/config/swe-runner.json`，也可以用插件环境变量
`SWE_RUNNER_CONFIG` 指向其他位置。字段、命令、SSH 密码变量和动作示例见
`plugins/swe-runner/README.md`。

swe-runner 依赖 nodes 插件提供的 `nodes` 服务。推荐让目标引用节点名称，由 nodes 插件
统一管理主机、用户、端口和密码环境变量：

```json
{
  "default": "69",
  "targets": {
    "69": {
      "node": "worker-01",
      "queue": "10.0.10.69",
      "remoteDir": "/mnt/jfs/tools/swe-runner"
    }
  }
}
```

使用 `node` 时不能再写 `host`、`user`、`passwordEnv`、`port` 或
`strictHostKeyChecking`；上述字段只用于兼容旧配置。

插件注册的 `type_name` 为 `swe-runner`。在 `config/actions.json` 中使用该类型即可调用：

```json
{
  "swe-status": {
    "type": "swe-runner",
    "target": "99",
    "history": false
  }
}
```

插件未挂载时框架仍可正常运行；如果挂载了 `swe-runner` 但 nodes 插件或 `nodes` 服务
缺失，启动会失败。配置不存在或默认目标未设置时，插件仍会加载但查询命令会提示先配置目标。

## 动作配置

动作使用固定 argv 数组，不经过 shell。参数占位符格式为 `{name}`：

```json
{
  "settings": {
    "maxConcurrency": 8,
    "maxOutputChars": 3600,
    "maxReplyChars": 12000
  },
  "actions": {
    "ping": {
      "description": "Ping 指定主机",
      "command": ["ping", "-c", "3", "-W", "2", "{host}"],
      "timeoutSeconds": 20,
      "parameters": {
        "host": {
          "description": "IPv4、IPv6 或域名",
          "required": true,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$"
        }
      }
    }
  },
  "batches": {
    "health": {
      "description": "基础健康检查",
      "actions": ["disk", "load", "uptime"]
    }
  }
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `command` | `command` provider 的必填非空 argv 字符串数组 |
| `type` | 动作 provider 类型，默认 `command`；框架内置 `command`，`node` 等类型由插件注册 |
| `remoteCommand` | 节点动作的远程命令，只能来自可信配置 |
| `parameters` | 参数定义，支持 `required`、`default`、`pattern`、`description`、`type`；`type` 由 provider 解释 |
| `timeoutSeconds` | 超时后终止整个子进程组 |
| `workingDir` | 工作目录，相对路径以动作文件所在目录为基准 |
| `env` | 追加给动作的固定环境变量 |
| `batches[].actions` | 预置动作名称列表 |

`node` 动作由 nodes 插件提供，要求显式声明 `node` 参数，且参数类型为 `node`。其他
provider 的配置字段由对应插件校验并解释，例如 swe-runner 插件的 `target` 和 `history`。

## 扩展动作类型

核心 `actions.py` 只负责加载、校验、并发调度和结果汇总，不包含具体动作类型逻辑。
业务 action 类型应放在独立仓库中，并挂载到 `plugins/` 目录。框架通过稳定接口
`dingtalk_ops_bridge.plugin_api` 和 `action_plugin.json` 自动发现 provider，不修改
`actions.py`、`commands.py` 或 `__main__.py`，也不需要重建镜像。

provider 需要实现两个方法：

- `validate(definition)`：校验该类型的配置，配置错误应在加载阶段失败。
- `prepare(definition, values, context)`：解析参数并返回 `PreparedAction`。

`PreparedAction` 可以返回普通进程命令，也可以提供自定义 `runner`，用于 HTTP API、
数据库等非进程执行器。provider 可以通过 `context.resources` 获取通用框架资源，例如
`RESOURCE_CONFIG_DIR`；插件私有配置应由插件自行读取。

插件可以在 manifest 中通过 `actionsFile` 自带动作和批处理 JSON，框架会把它们与
`config/actions.json` 合并。插件文件中的 `settings` 会被忽略，action/batch 名称不能重复。
nodes 插件的节点动作和 `node-health` 批处理就是这样分发的。

插件还可以在 manifest 中通过 `commandProvider` 注册命令处理器，在 `run`、`batch`
等框架命令之前接收钉钉文本。命令处理器必须返回 Markdown 字符串，或返回 `None`
表示不接管该命令。nodes 插件用这种方式提供 `nodes` 命令，swe-runner 插件用这种方式
兼容 `status`、`history` 和 `69 status` 等旧 bridge 命令。

插件间依赖使用 manifest 的 `services`/`requires`。加载器会拓扑排序并在启动时拒绝缺失
服务、重复服务和循环依赖；消费者通过 `context.require_service("<名称>")` 调用，不能
跨插件目录 import。例如 swe-runner 声明 `requires: ["nodes"]`，nodes 插件声明
`services: {"nodes": "SERVICE"}`。

最小 provider 结构如下：

```python
from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION, PreparedAction


class MyProvider:
    api_version = PLUGIN_API_VERSION
    type_name = "my-action"

    def validate(self, definition):
        pass

    def prepare(self, definition, values, context):
        return PreparedAction(definition=definition, command=("echo", "ok"))


PROVIDER = MyProvider()
```

插件仓库目录、manifest 字段、版本规则和安全约束见 `docs/ACTION_PLUGINS.md`。
新增或更新插件后只需重启服务，不需要重新构建镜像：

```bash
docker compose restart bridge monitor
```

## 监控配置

常用格式：

```json
{
  "settings": {
    "maxConcurrency": 8,
    "defaultIntervalSeconds": 60,
    "maxOutputChars": 4000
  },
  "monitors": {
    "api-health": {
      "description": "HTTP 健康检查",
      "check": {
        "type": "http",
        "url": "https://example.com/health",
        "method": "GET",
        "expectedStatus": [200],
        "bodyContains": ["ok"]
      },
      "intervalSeconds": 60,
      "timeoutSeconds": 10,
      "severity": "critical",
      "repeatAlertSeconds": 900,
      "notifyRecovery": true,
      "enabled": true
    }
  }
}
```

通用字段：

| 字段 | 说明 |
| --- | --- |
| `intervalSeconds` | 检查间隔 |
| `timeoutSeconds` | 单次检查超时 |
| `severity` | `info`、`warning` 或 `critical` |
| `repeatAlertSeconds` | 持续异常时的重复提醒间隔；`0` 表示仅在状态变化时提醒 |
| `notifyRecovery` | 是否发送恢复通知 |
| `enabled` | 是否启用 |

### HTTP 检查

| 字段 | 说明 |
| --- | --- |
| `url` | 必填，请求地址 |
| `method` | 默认 `GET` |
| `headers` | 请求头对象 |
| `expectedStatus` | 允许的状态码，支持整数或数组 |
| `bodyContains` | 响应体必须包含的字符串，支持字符串或数组 |

### TCP 检查

| 字段 | 说明 |
| --- | --- |
| `host` | 目标主机 |
| `port` | 目标端口，1-65535 |

### 命令检查

| 字段 | 说明 |
| --- | --- |
| `command` | 必填，argv 数组 |
| `expectedExitCodes` | 允许的退出码，默认 `[0]` |
| `stdoutContains` | 标准输出必须包含的字符串 |
| `stderrContains` | 标准错误必须包含的字符串 |
| `workingDir` | 工作目录 |
| `env` | 追加环境变量 |

命令检查也建议使用 argv 数组；需要 shell 管道时显式写成 `["sh", "-c", "..."]`，并确保命令内容来自可信配置，而不是钉钉输入。

## 告警与状态

- 正常到失败：立即发送异常通知。
- 持续失败：`repeatAlertSeconds > 0` 时按间隔重复；否则不重复。
- 失败到正常：`notifyRecovery` 为 `true` 时发送恢复通知。
- Webhook 发送失败：保留未成功发送的告警状态，下个检查周期重试。
- 状态文件采用临时文件加原子替换写入，避免进程中断产生半写 JSON。

## 验证命令

```bash
PYTHONPATH=src python3 -m dingtalk_ops_bridge validate
PYTHONPATH=src python3 -m dingtalk_ops_bridge command help
PYTHONPATH=src python3 -m dingtalk_ops_bridge monitor --once --dry-run
```
