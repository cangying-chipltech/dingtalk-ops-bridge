# Action Plugin API

框架镜像只包含调度、配置加载、监控和钉钉连接能力。Action 类型和自定义命令由独立
仓库实现，以只读目录挂载到 `/app/plugins`，框架启动时自动发现。新增或升级插件不需要
重建框架镜像，只需更新插件仓库并重启服务。

## 版本契约

当前插件 API 版本为 `1`：

```python
from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION
```

Action provider 必须同时满足：

- `api_version == PLUGIN_API_VERSION`
- 定义唯一的 `type_name`
- 实现 `validate(definition)` 和 `prepare(definition, values, context)`

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
    src/
      my_actions/
        __init__.py
    tests/
      test_my_actions.py
```

插件测试应与插件代码放在同一仓库目录。框架的 `tests/` 只保留核心调度、配置加载和
插件机制测试；统一执行 `./scripts/run-tests.sh` 会依次发现框架、nodes 和 swe-runner
的测试目录。

`action_plugin.json`：

```json
{
  "apiVersion": 1,
  "id": "my-actions",
  "module": "my_actions",
  "provider": "PROVIDER",
  "commandProvider": "COMMANDS",
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
- `services`：可选，服务名到模块属性名的映射；服务对象必须定义 `api_version`。
- `requires`：可选，依赖的服务名数组；加载器保证提供者先加载。
- `actionsFile`：可选，插件自带 actions/batches 定义文件，相对插件根目录；也可以是路径数组。
- `pythonPath`：可选，相对插件根目录的 Python 搜索路径，可以是字符串或字符串数组。

`actionsFile` 与框架 `config/actions.json` 结构相同，但插件文件中的 `settings` 会被忽略，
只合并 `actions` 和 `batches`；重名会在启动时失败。这样 action 定义可以跟随插件仓库
独立分发，不需要修改框架配置。

一个插件可以只提供 action provider、command provider、service provider、actionsFile，
或组合使用。未声明任何扩展点时启动失败。

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

## 命令 Provider

`commandProvider` 用于兼容旧机器人命令或在框架命令之前增加专用查询。它只要求实现：

```python
from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION


class Commands:
    api_version = PLUGIN_API_VERSION

    def handle(self, prompt, context):
        if prompt.strip() != "my-check":
            return None
        return "### my-check\n\n结果正常"


COMMANDS = Commands()
```

`handle()` 返回 Markdown 字符串表示接管命令，返回 `None` 表示继续交给框架。可以提供
可选的 `help_lines()` 方法，返回字符串或字符串列表，用于汇总到 `help` 输出。命令
provider 按加载顺序调用，不应抢占 `run`、`batch`、`monitor` 等框架命令。

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

`validate` 会显示已注册的动作类型和命令 provider 数量。框架也支持 Python entry point
`dingtalk_ops_bridge.actions` 和 `dingtalk_ops_bridge.commands`，但固定镜像场景优先使用
目录 manifest 方式。

本地开发插件时，把框架的 `src/` 加入 `PYTHONPATH`，或在虚拟环境中安装框架包，以便
导入 `dingtalk_ops_bridge.plugin_api`。容器运行时由框架镜像直接提供该接口。

## 约束与安全

- 插件代码在容器内拥有与框架相同的权限，只挂载可信仓库。
- 插件目录默认只读，插件不应在自身目录写状态；需要持久化时写入 `/app/data`。
- `type_name` 必须全局唯一；与内置或已有插件冲突时启动失败。
- 插件只在进程启动时扫描；新增或修改插件后需要重启 `bridge` 和 `monitor`。

框架只内置 `command` provider。`plugins/nodes/` 和 `plugins/swe-runner/` 都是同时
提供 action provider 与 command provider 的示例；其他业务 action 类型和兼容命令应放在
独立仓库中，通过新的 `type_name` 或 manifest 注册。
