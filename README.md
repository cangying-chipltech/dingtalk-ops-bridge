# DingTalk Ops Bridge

一个简单可验证的钉钉运维桥：

- 接收钉钉固定指令，并发执行白名单动作并返回结果。
- 周期执行内置或插件提供的监控，异常和恢复时发送钉钉 Webhook。
- 框架镜像固定，action 以独立仓库挂载到 `plugins/`，新增执行器无需重建镜像。
- 不需要大模型，不需要第三方 Python 依赖。

## 先看效果

本地端到端演示不连接真实钉钉，也不需要 Webhook 凭据：

```bash
cd /home/cangying/dingtalk-ops-bridge
./scripts/run-e2e.sh
```

脚本会启动一个本地模拟 Webhook 服务，并依次验证 7 个案例：

演示直接调用 `dws` 使用的 `scripts/agent-router.sh` 入口，不覆盖钉钉网络传输本身。

| 案例 | 验证内容 |
| --- | --- |
| 1 | `run hello` 经命令路由执行动作并返回 Markdown |
| 2 | 两个耗时动作并发执行，而不是串行等待 |
| 3 | 钉钉 `check` 指令触发即时监控检查，并加载插件监控项 |
| 4 | 监控失败后收到 Webhook 异常消息 |
| 5 | 持续异常且 `repeatAlertSeconds=0` 时不重复告警 |
| 6 | 监控恢复后收到 Webhook 恢复消息，状态更新为正常 |
| 7 | 外部插件目录同时注册 action、command 和 monitor provider |

预期结尾：

```text
All 7 end-to-end cases passed.
```

## 最小部署

主框架和插件分仓库维护。首次部署时先克隆插件仓库：

```bash
git clone git@github.com:cangying-chipltech/dingtalk-ops-plugins.git plugins
```

### 1. 配置钉钉

```bash
cp .env.example .env
```

在 `.env` 中配置一种钉钉连接方式：

```text
DWS_UNIFIED_APP_ID=<unified app id>
```

或：

```text
ROBOT_CLIENT_ID=<robot app key>
ROBOT_CLIENT_SECRET=<robot app secret>
```

同时配置异常上报：

```text
DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=...
DINGTALK_WEBHOOK_SECRET=<可选加签密钥>
DINGTALK_WEBHOOK_KEYWORD=<可选关键词>
```

### 2. 配置动作和监控

默认动作在 `config/actions.json`。框架内置监控为空，需要时从模板创建：

```bash
cp config/monitors.example.json config/monitors.json
```

修改模板中的 URL、端口或检查命令，只保留需要的监控项。

模型日志和节点服务等业务监控由插件自带。当前 `plugins/model-manager/` 会根据
`plugins/model-manager/config/models.json` 生成 vLLM 日志监控和 `new-api` 服务监控，
不再写入 `config/monitors.json`。

### 3. 启动

```bash
docker compose up -d --build
docker compose ps
```

查看日志：

```bash
docker logs -f dingtalk-ops-bridge
docker logs -f dingtalk-ops-monitor
```

## 钉钉命令

| 指令 | 作用 |
| --- | --- |
| `help` | 显示命令菜单 |
| `list` | 列出动作和批处理 |
| `run disk,memory,uptime` | 并发执行多个动作 |
| `run ping host=10.0.0.12` | 执行带参数动作 |
| `nodes` | 节点插件：列出节点，不显示密码 |
| `run node-login node=web-01` | 验证节点 SSH 登录 |
| `batch node-health node=web-01` | 检查节点登录、基础信息和磁盘 |
| `batch health` | 执行预置动作组 |
| `model list` | 模型管理插件：列出模型和关联监控项 |
| `model check vllm-27` | 模型管理插件：立即检查模型日志 |
| `monitor list` | 列出监控项 |
| `monitor check newapi-status-205` | 检查 205 的 `new-api` 容器和 API |
| `monitor check api-health` | 立即执行指定检查 |
| `status` | 立即执行全部已启用监控 |
| `bridge status` | 显式执行框架监控；即使插件占用 `status` 仍可用 |
| `swe-runner status` / `swe-runner history` | swe-runner 插件默认目标查询 |
| `69 status` / `10.0.10.69 history` | 兼容旧 bridge，按插件目标 key 或别名查询 |
| `check` | 立即执行全部已启用监控，始终由框架处理 |

## 配置入口

| 需要修改 | 文件 |
| --- | --- |
| 钉钉凭据、Webhook、并发和超时 | `.env` |
| 可执行动作和批处理 | `config/actions.json` |
| 节点登录信息 | `plugins/nodes/config/nodes.json` |
| 模型实例、日志和节点服务监控 | `plugins/model-manager/config/models.json` |
| swe-runner 插件目标 | `plugins/swe-runner/config/swe-runner.json` |
| 框架静态监控项 | `config/monitors.json` |
| 完整配置字段 | `docs/CONFIGURATION.md` |
| 部署、安全和排障 | `docs/OPERATIONS.md` |

默认动作包括 `disk`、`memory`、`load`、`uptime`、`processes`、`ping`，以及
`node-login`、`node-info`、`node-disk` 三个节点动作。节点动作由
`plugins/nodes/config/actions.json` 随插件自动合并。节点清单默认为空，可参考
`plugins/nodes/config/nodes.example.json` 添加。

动作采用白名单 argv 执行，不接受任意 shell 命令。参数必须声明并通过正则校验，超时后会终止子进程组。

### nodes 插件

节点清单、`node` 动作类型和 `nodes` 命令都由 `plugins/nodes/` 提供，不属于框架核心。
配置位于 `plugins/nodes/config/nodes.json`，支持密码环境变量、密码文件和 SSH 私钥认证。
`passwordFile` 使用 `sshpass -f` 在每次执行时读取，轮换密码无需重启服务。命令：

```text
nodes
node list
run node-login node=web-01
```

节点插件会校验节点名称并隐藏密码环境变量值。配置格式和认证说明见
`plugins/nodes/README.md`。旧 `NODES_FILE` 环境变量仍可用于指定节点文件。

### model-manager 插件

模型清单、日志路径、节点服务和告警关键字由 `plugins/model-manager/` 管理。插件通过
`monitorProvider` 为每个模型生成日志监控、为每个服务生成容器与 HTTP 健康监控，并通过
`requires: ["nodes"]` 复用节点登录信息。配置位于
`plugins/model-manager/config/models.json`，命令示例：

```text
model list
model check vllm-27,vllm-31
monitor check newapi-status-205
```

修改模型、服务、节点或日志关键字后重启 `bridge monitor` 即可。配置格式和监控说明见
`plugins/model-manager/README.md`。

### swe-runner 插件

swe-runner 不再属于框架核心，而是 `plugins/swe-runner/` 下的示例插件。它同时注册
`swe-runner` 动作类型和旧 bridge 命令兼容层，并通过 `requires: ["nodes"]` 复用 nodes
插件的节点登录信息。配置位于 `plugins/swe-runner/config/swe-runner.json`，目标使用
`node` 引用节点，同时兼容旧的 `host`、`user`、`passwordEnv` 写法。挂载插件后可直接使用：

```text
status
history
69 status
10.0.10.69 history
query
recent
swe-runner check
```

插件状态命令使用 `swe-runner` 前缀，例如 `swe-runner status`、`swe-runner history`、
`swe-runner query`。裸 `status` 和 `check` 始终执行监控检查；若插件显式占用 `status`，
仍可用 `bridge status` 调用框架监控。旧 bridge 的 `69 status` 写法继续兼容。配置和
密码变量说明见 `plugins/swe-runner/README.md`。

## Action 插件

业务 action 和业务监控建议放在独立仓库中，通过只读目录挂载到 `plugins/`。每个仓库提供
`action_plugin.json`、稳定接口 provider，并可通过 `actionsFile` 自带动作与批处理定义，
或通过 `monitorProvider` 自带监控定义。框架启动时自动合并，不需要修改或重建框架镜像。
`nodes`、`swe-runner` 和 `model-manager` 是完整示例。
插件之间通过 manifest 的 `services`/`requires` 声明依赖，按服务名拓扑加载；插件代码
通过 `context.require_service()` 调用，不跨目录 import。完整契约、仓库结构和部署方式
见 `docs/ACTION_PLUGINS.md`。

命令 provider 应使用 `command_prefixes` 声明首 token 路由；需要兼容动态命令时可使用
唯一的 `command_fallback = True`。重复前缀、抢占 `run`/`batch`/`monitor`/`check` 等
框架保留命令，或注册多个 fallback，都会在服务启动时直接报错，不会按加载顺序静默覆盖。

## 项目结构

```text
config/                  动作、监控和模板
data/                    监控状态持久化
docs/                    详细配置与运维说明
e2e/                     端到端案例
plugins/                 外部 action/命令插件挂载目录；每个插件自带 README 和 tests/
scripts/                 钉钉入口和 E2E 启动脚本
src/dingtalk_ops_bridge/ 命令、动作、检查、监控和 Webhook 实现
src/dingtalk_ops_bridge/action_types/ 内置动作 provider
tests/                   框架核心与插件机制单元测试
```

## 验证

```bash
./scripts/run-e2e.sh
./scripts/run-tests.sh
set -a; . ./.env; set +a
PYTHONPATH=src python3 -m dingtalk_ops_bridge validate
docker compose config
```

本地直接运行 Python 命令时需要先加载 `.env`；Docker Compose 会自动加载。

如果有 `make`，也可以使用 `make e2e`、`make test` 和 `make validate`。`make test`
会分别执行框架、nodes、model-manager 和 swe-runner 插件测试。

## 安全提示

`.env` 含有凭据和 Webhook 密钥，不要提交或写入镜像。详细安全建议、非 root 运行、Webhook 安全设置和故障排查见 `docs/OPERATIONS.md`。
