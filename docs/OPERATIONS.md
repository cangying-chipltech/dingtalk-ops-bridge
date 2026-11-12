# 部署与运维

## 启动与停止

```bash
cp .env.example .env
# 编辑 .env
docker compose up -d --build
docker compose ps
docker logs -f dingtalk-ops-bridge
docker logs -f dingtalk-ops-monitor
```

停止：

```bash
docker compose down
```

仅在配置变化后重建：

```bash
docker compose up -d --force-recreate
```

`config/` 以只读方式挂载，修改动作、节点、兼容目标或监控配置后无需重建镜像；下一次命令请求或监控周期会读取新配置。

## 数据与卷

| 路径 | 用途 |
| --- | --- |
| `./config:/app/config:ro` | 动作、节点、兼容目标和监控配置 |
| `./data:/app/data` | 监控状态、known_hosts 和可选 SSH 私钥 |
| `dws-data:/var/lib/dws` | DWS 认证状态 |
| `dws-keychain:/root/.local/share/dws-cli` | DWS keychain 兼容目录 |

重新初始化监控状态：

```bash
docker compose stop monitor
rm -f data/monitor-state.json
docker compose start monitor
```

删除状态后，下一次失败会被视为首次异常并重新告警。

## 本地测试

快速端到端案例：

```bash
./scripts/run-e2e.sh
```

单元测试（框架 + 各插件目录）：

```bash
./scripts/run-tests.sh
```

只跑某个插件时，从项目根目录执行对应目录的发现命令，例如：

```bash
PYTHONPATH=src python3 -m unittest discover -s plugins/nodes/tests -v
PYTHONPATH=src python3 -m unittest discover -s plugins/swe-runner/tests -v
```

本地执行需要读取 `.env` 的命令前，先加载环境变量：

```bash
set -a; . ./.env; set +a
```

配置校验：

```bash
PYTHONPATH=src python3 -m dingtalk_ops_bridge validate
docker compose config
```

## 安全建议

- `.env` 包含钉钉凭据和 Webhook 密钥，不要提交或写入镜像。
- 优先使用钉钉加签、关键词或 IP 白名单中的至少一种 Webhook 安全方式。
- 动作是白名单执行模型，但参数仍应使用严格正则和较小取值范围。
- 不要在 `sh -c` 字符串中直接拼接钉钉输入；需要 shell 时，让脚本接收独立 argv 并自行校验。
- 节点密码只通过 `passwordEnv` 引用 `.env` 中的变量；节点 JSON 中出现明文 `password` 会被拒绝。
- SSH 私钥放在 `data/ssh/` 并设为 `600`，不要放进 `config/`，否则会被构建进镜像。
- 生产环境建议使用 `strictHostKeyChecking=yes` 并提前维护 known_hosts；`accept-new` 适合首次接入。
- 为每个动作设置最小必要超时，避免任务长期占用机器人连接。
- 容器当前默认以 root 运行。使用非 root 用户时需同时调整 `/var/lib/dws`、`/app/data` 和 home 目录权限。
- 镜像构建会从 Gitee 下载 `dws` 安装脚本。生产环境建议固定可信版本或使用内部镜像源。

## 故障排查

### 钉钉消息没有反馈

1. 查看 bridge 日志，确认 `dws dev connect` 已连接。
2. 在容器内或宿主机执行：

```bash
./scripts/agent-router.sh help
```

3. 检查 `DWS_UNIFIED_APP_ID`，或 `ROBOT_CLIENT_ID` 与 `ROBOT_CLIENT_SECRET` 是否成对配置。
4. 检查 `DWS_ALLOWED_USERS`、`DWS_ALLOWED_GROUPS` 是否过滤掉了当前用户或群。

### 监控不发送 Webhook

1. 执行 `PYTHONPATH=src python3 -m dingtalk_ops_bridge validate`。
2. 确认 `DINGTALK_WEBHOOK_URL` 已设置。
3. 执行 `PYTHONPATH=src python3 -m dingtalk_ops_bridge monitor --once --dry-run` 查看告警内容。
4. 执行 `PYTHONPATH=src python3 -m dingtalk_ops_bridge webhook-test` 验证真实网络发送。
5. 检查钉钉机器人的加签、关键词和 IP 白名单设置。

### 持续收到重复告警

把对应监控项的 `repeatAlertSeconds` 设为 `0`，只在状态变化时通知。异常恢复后会发送恢复消息。

### 监控结果与预期不一致

先用 `status` 或 `monitor check <名称>` 立即执行检查，再检查 `timeoutSeconds`、`expectedStatus`、`expectedExitCodes` 和命令运行环境。

### Action 插件未加载

1. 执行 `docker compose exec bridge python3 -m dingtalk_ops_bridge validate`，确认输出中的 `types` 包含插件类型。
2. 确认插件仓库位于宿主机 `plugins/` 目录下，并存在 `action_plugin.json`。
3. 检查 manifest 的 `apiVersion`、`module`、`provider`、`commandProvider`、`services`、`requires` 和 `pythonPath`。
4. 查看 bridge 日志中的 provider 注册或导入错误。
5. 插件只在启动时扫描，修改后执行 `docker compose restart bridge monitor`。

### 插件依赖服务缺失

1. 执行 `validate`，确认输出中的 `services` 包含被依赖的服务。
2. 检查消费方 manifest 的 `requires` 与提供方 `services` 名称是否完全一致。
3. 依赖服务缺失、重复提供或存在循环时，启动会失败；修复后重启服务。
4. 插件代码应通过 `context.require_service("<名称>")` 调用，不要跨目录 import。

### 节点登录失败

1. 确认 `plugins/nodes/action_plugin.json` 存在且插件目录已挂载到 `/app/plugins`。
2. 执行 `docker compose exec bridge python3 -m dingtalk_ops_bridge validate`，确认 `types` 包含 `node`。
3. 执行 `nodes` 确认节点名称，检查 `plugins/nodes/config/nodes.json` 是否已配置。
4. 执行 `run node-login node=<名称>` 查看 SSH 错误。
5. 检查 `.env` 中对应的密码环境变量是否已设置。
6. 使用私钥时确认 `/app/data/ssh/` 权限为 `700`、私钥为 `600`。
7. 使用 `strictHostKeyChecking=yes` 时确认 `/app/data/known_hosts` 已包含目标主机公钥。

### 旧 swe-runner 命令失败

1. 确认 `plugins/swe-runner/action_plugin.json` 存在且插件目录已挂载到 `/app/plugins`。
2. 执行 `docker compose exec bridge python3 -m dingtalk_ops_bridge validate`，确认 `types` 包含 `swe-runner` 且 `commands` 大于 `0`。
3. 检查 `plugins/swe-runner/config/swe-runner.json` 的 `default` 是否存在于 `targets`。
4. 使用 `node` 时，确认节点存在于 `plugins/nodes/config/nodes.json`；使用旧格式时检查 `host`、`user` 是否正确。
5. 确认 `queue`、`remoteDir` 正确，且节点插件的 `passwordEnv` 指向的变量已设置在 `.env`。
6. 确认目标机器存在 `<remoteDir>/status_swebench.sh` 并且可执行。
7. 使用 `<目标> status` 测试指定目标；监控检查使用 `check`，插件不会接管裸 `check`。
