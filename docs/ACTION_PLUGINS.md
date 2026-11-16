# Action Plugin API

框架镜像只包含调度、配置加载、监控和钉钉连接能力。Action 类型、自定义命令和监控
provider 由独立仓库实现，以只读目录挂载到 `/app/plugins`，框架启动时自动发现。
新增或升级插件不需要重建框架镜像，只需更新插件仓库并重启服务。

## 版本契约

当前插件 API 版本为 `1`：

```python
from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION
```

Action provider 必须同时满足：

- `api_version == PLUGIN_API_VERSION`
- 定义唯一的 `type_name`
- 实现 `validate(definition)` 和 `prepare(definition, values, context)`

Monitor provider 必须同时满足：

- `api_version == PLUGIN_API_VERSION`
- 定义唯一的 `type_name`
- 实现 `monitors(context)`、`validate(check)` 和 `check(check, context)`

`plugin_api.py` 是稳定导入入口。插件不要导入 `actions.py`、`action_types/` 等内部模块。
同一 API 版本内只做向后兼容的增量修改；破坏性变更会提升 `PLUGIN_API_VERSION`。

## 插件仓库结构

每个 action 仓库放在 `plugins/` 下的独立子目录。仓库根目录包含
`action_plugin.json`：

```text
plugins/
  my-actions/
    action_plugin.json
    config/
      actions.json
      models.json
    src/
      my_actions/
        __init__.py
    tests/
      test_my_actions.py
```

插件测试应与插件代码放在同一仓库目录。框架的 `tests/` 只保留核心调度、配置加载和
插件机制测试；统一执行 `./scripts/run-tests.sh` 会依次发现框架和各插件的测试目录。

`action_plugin.json`：

```json
{
  "apiVersion": 1,
  "id": "my-actions",
  "module": "my_actions",
  "provider": "PROVIDER",
  "commandProvider": "COMMANDS",
  "monitorProvider": "MONITORS",
  "services": {"my-service": "SERVICE"},
  "requires": ["nodes"],
  "actionsFile": "config/actions.json",
  "pythonPath": "src"
}
```

- `apiVersion`：必填，必须等于框架的插件 API 版本。
- `id`：可选，插件唯一标识，默认使用目录名；用于依赖诊断和重复检测。
- `module`：必填，Python 模块名。
- `provider`：可选，模块中的 provider 属性名，默认 `PROVIDER`；也可以是属性名数组。
- `commandProvider`：可选，模块中的命令 provider 属性名；也可以是属性名数组。
- `monitorProvider`：可选，模块中的监控 provider 属性名；也可以是属性名数组。
- `services`：可选，服务名到模块属性名的映射；服务对象必须定义 `api_version`。
- `requires`：可选，依赖的服务名数组；加载器保证提供者先加载。
- `actionsFile`：可选，插件自带 actions/batches 定义文件，相对插件根目录；也可以是路径数组。
- `pythonPath`：可选，相对插件根目录的 Python 搜索路径，可以是字符串或字符串数组。

`actionsFile` 与框架 `config/actions.json` 结构相同，但插件文件中的 `settings` 会被忽略，
只合并 `actions` 和 `batches`；重名会在启动时失败。这样 action 定义可以跟随插件仓库
独立分发，不需要修改框架配置。

一个插件可以只提供 action provider、command provider、monitor provider、service
provider、actionsFile，或组合使用。未声明任何扩展点时启动失败。

如果模块直接放在插件根目录，可以省略 `pythonPath`。需要第三方依赖时，建议把依赖
vendor 到插件仓库并加入 `pythonPath`，因为框架镜像不会为单个插件重建。插件模块名
应全局唯一，避免多个仓库使用相同模块名。

## 插件依赖与服务

插件依赖按“服务契约”管理，不按目录名或 Python import 管理。提供方在 manifest 的
`services` 中声明服务，消费方在 `requires` 中声明服务名。加载器会做拓扑排序；缺失
服务、重复服务或循环依赖都会在启动时失败，不会等到动作执行时才暴露。

```json
{
  "apiVersion": 1,
  "id": "swe-runner",
  "module": "swe_runner_plugin",
  "provider": "PROVIDER",
  "commandProvider": "COMMAND_PROVIDER",
  "requires": ["nodes"],
  "pythonPath": "src"
}
```

服务对象示例：

```python
from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION


class Service:
    api_version = PLUGIN_API_VERSION

    def query(self, name, context):
        return {"name": name}


SERVICE = Service()
```

消费方不能跨插件目录 `import`，应通过运行时上下文调用：

```python
service = context.require_service("nodes")
node = service.resolve("web-01", context)
```

当前 `nodes` 服务契约由 nodes 插件定义：

- `list_nodes(context)`：返回节点信息列表，不包含密码。
- `resolve(name, context)`：按名称返回 `NodeInfo`，不存在时返回 `None`。
- `ssh_command(name, remote_command, context)`：返回 `SshCommand`，其中包含 `argv`、
  `env` 和 `node`（`NodeInfo`）；密码使用环境变量占位符，不进入命令行。

`NodeInfo` 提供 `name`、`host`、`user`、`port`、`description` 和 `auth_method`。
只要替代插件遵守同一服务契约，swe-runner 无需修改。

## Provider 实现

最小 provider：

```python
from dingtalk_ops_bridge.plugin_api import (
    PLUGIN_API_VERSION,
    ActionValidationError,
    PreparedAction,
)


class MyProvider:
    api_version = PLUGIN_API_VERSION
    type_name = "my-command"

    def validate(self, definition):
        if not definition.config.get("message"):
            raise ActionValidationError("my-command requires 'message'")

    def prepare(self, definition, values, context):
        message = str(definition.config["message"])
        return PreparedAction(
            definition=definition,
            command=("printf", "%s", message),
        )


PROVIDER = MyProvider()
```

然后可以在 `config/actions.json` 中使用：

```json
{
  "hello-plugin": {
    "type": "my-command",
    "description": "外部仓库提供的动作",
    "message": "hello"
  }
}
```

执行方式仍是 `run hello-plugin`。动作的并发、超时、输出截断和 Markdown 反馈由框架统一处理。

## PreparedAction

`prepare()` 返回 `PreparedAction`，支持两种执行方式：

1. 返回 `command` 和 `env`：框架使用标准进程执行器，并应用 `timeoutSeconds`、
   `workingDir` 和输出限制。
2. 返回 `runner(max_output_chars)`：插件自行执行 HTTP、数据库、云 API 等操作，并返回
   `ActionResult`。这类插件负责自己的超时和异常处理。

插件可以通过 `context.resources` 读取通用框架资源，通过 `context.require_service()`
调用已声明的插件服务。例如：

- `RESOURCE_CONFIG_DIR`：框架配置目录路径
- `context.require_service("nodes")`：获取 nodes 插件服务

插件私有配置应放在插件仓库内，并由插件自行读取环境变量或配置文件；不要让框架核心
传递具体业务的 catalog 对象。

## 命令 Provider 与冲突规则

`commandProvider` 用于兼容旧机器人命令或增加专用查询。命令按首 token 显式路由，
不再依赖插件加载顺序：

```python
from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION


class Commands:
    api_version = PLUGIN_API_VERSION
    command_prefixes = ("my-check",)

    def handle(self, prompt, context):
        return "### my-check\n\n结果正常"


COMMANDS = Commands()
```

- `command_prefixes`：声明该 provider 接管的命令首 token，大小写不敏感，例如
  `("nodes", "node", "节点")`；不能包含空格。
- `command_fallback = True`：仅在命令未被任何前缀声明、且不是框架保留命令时调用。
  同一进程最多允许一个 fallback provider；动态目标命令可使用这种方式。
- 未声明 `command_prefixes` 的旧 provider 会按唯一 fallback 兼容；若同时存在多个，
  启动失败并提示插件来源。
- 重复前缀、抢占框架保留命令都会在启动期失败，错误信息包含冲突双方的来源。
- 框架保留 `help`、`list`、`run`、`batch`、`monitor`、`bridge`、`check` 及其别名。
  插件自己的状态命令应使用插件前缀，例如 `swe-runner status`。
- 插件可以显式声明 `status` 接管裸命令，但 `bridge status` 始终由框架保留，可在任何
  情况下显式执行监控检查。插件不应声明 `check`。
- 命中显式前缀后，该 provider 是唯一处理者；`handle()` 返回 `None` 会视为命令未处理并
  返回明确错误，不再交给框架或 fallback。`handle()` 返回 Markdown 字符串表示命令已完成。
- 只有未命中前缀、进入 fallback 的 provider 返回 `None` 时，框架才继续处理该命令。

`help_lines()` 可选，返回字符串或字符串列表，用于汇总到 `help` 总览。插件还可以实现
`help(topic, context)`，由框架把 `help <插件前缀> ...` 的完整主题交给对应插件；这样
`help model add`、`help alert add model-log` 等子命令帮助由插件自行维护，框架不需要
硬编码业务命令。`help()` 返回字符串；不支持的子主题返回 `None`，框架会回退到
`help_lines()` 或提示未知主题。

总帮助会根据注册的 `command_prefixes` 自动生成插件帮助入口，例如
`help alert`、`help node`、`help swe-runner`。新增插件只要声明前缀，就会自动出现在
总帮助中，不需要修改框架。

```python
def help(self, topic, context):
    if topic == "my-check status":
        return "### my-check status\n\n检查目标状态。"
    return None
```

swe-runner 使用 `command_prefixes = ("swe-runner",)` 承接带插件前缀的命令，并用唯一
fallback 兼容 `69 status` 这类由配置目标名决定的旧命令。

## Monitor Provider

`monitorProvider` 让插件自带监控项和检查逻辑。框架在加载监控目录时调用
`monitors(context)`，再把返回的定义合并到核心监控目录；同名监控、未知类型或校验失败
都会在启动期报错。

```python
from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION, MonitorResult


class Monitors:
    api_version = PLUGIN_API_VERSION
    type_name = "my-monitor"

    def monitors(self, context):
        return {
            "my-plugin-health": {
                "description": "插件自有健康检查",
                "check": {"type": self.type_name, "target": "primary"},
                "intervalSeconds": 60,
                "severity": "critical",
            }
        }

    def validate(self, check):
        if not check.get("target"):
            raise ValueError("target is required")

    def check(self, check, context):
        nodes = context.require_service("nodes")
        target = nodes.resolve(check["target"], context)
        if target is None:
            return MonitorResult(False, "target not found")
        return MonitorResult(True, "healthy")


MONITORS = Monitors()
```

`monitors()` 返回的每一项与 `config/monitors.json` 中的监控定义格式相同；`check.type`
使用 provider 的 `type_name`。监控名称建议带插件前缀。插件可以通过
`context.require_service()` 使用 manifest 中声明的依赖，例如 nodes 服务的节点和 SSH
配置。告警去重、恢复通知、并发和状态持久化仍由框架统一处理。

完整示例见 `plugins/model-manager/`：它根据 `config/models.json` 为每个模型生成日志监控。

## 加载与部署

Compose 将 `./plugins` 只读挂载到 `/app/plugins`。更新插件后只需重启服务：

```bash
git clone <actions-repo-url> plugins/my-actions
docker compose restart bridge monitor
```

验证插件是否被识别：

```bash
docker compose exec bridge python3 -m dingtalk_ops_bridge validate
docker compose exec bridge ./scripts/agent-router.sh list
```

`validate` 会显示已注册的动作类型、命令 provider 数量、`command-routes`、
`monitor-types` 和是否存在 `command-fallback`。框架也支持 Python entry point
`dingtalk_ops_bridge.actions` 和 `dingtalk_ops_bridge.commands`，但固定镜像场景优先使用
目录 manifest 方式。

本地开发插件时，把框架的 `src/` 加入 `PYTHONPATH`，或在虚拟环境中安装框架包，以便
导入 `dingtalk_ops_bridge.plugin_api`。容器运行时由框架镜像直接提供该接口。

## 约束与安全

- 插件代码在容器内拥有与框架相同的权限，只挂载可信仓库。
- 插件目录默认只读，插件不应在自身目录写状态；需要持久化时写入 `/app/data`。
- Action 和 monitor provider 的 `type_name` 必须全局唯一；与内置或已有插件冲突时启动失败。
- 插件监控名称不能与核心配置或其他插件重复。
- 命令前缀必须全局唯一；冲突、框架保留命令或多个 fallback 都会导致启动失败。
- 插件只在进程启动时扫描；新增或修改插件后需要重启 `bridge` 和 `monitor`。

框架只内置 `command` 动作 provider 和 `http`/`tcp`/`command` 监控检查。`plugins/nodes/`
和 `plugins/swe-runner/` 是 action/command 插件示例，`plugins/model-manager/` 是
monitor provider 示例；其他业务类型应放在独立仓库中，通过新的 `type_name` 或 manifest 注册。
