# OTA Web Agent

Python SDK、命令行工具、FastAPI HTTP 服务及 MCP，直接调用现有 OTA 管理接口，使用 P12 双向 TLS、账号登录及 Cookie 会话。运行环境为 Python 3.11+，支持 macOS / Linux。

AI Agent 接入见 [MCP 设计与使用](docs/mcp-design.md)：`uv run ota mcp` 启动本地 stdio，或使用带令牌的 Streamable HTTP。用途部署默认只要求确认设备型号及当前 UFS 定制情况。

已实现：

- 分页查询、设备 / 版本 / 备注等筛选，读取完整多行备注。
- ZIP 上传新增，核验新任务并使其最终处于禁用状态。
- 单条及批量删除，**仅允许当前备注包含大小写一致的 `PATRICK` 的任务**。
- 增减 SN 或 SN 区间，保留前导零、去重、合并及拆分区间。
- 设置启用 / 禁用，推送独立控制且默认关闭。
- 获取设备更新接口 JSON，核对返回版本和下载路径中的任务 ID，不下载远程固件 ZIP。
- 查询、写入、登录、退出、鉴权失败、校验失败和上游请求均记录本地 JSONL 审计日志。

## 安装与配置

```bash
cd /Users/patrick/ota-web-agent
uv sync --extra dev
cp .env.example .env
chmod 600 .env
uv run ota token
```

将账号、密码、P12 绝对路径、证书密码和生成的 API 令牌填入 `.env`；也可以由部署环境注入同名环境变量。项目不附带或硬编码真实密码、证书和令牌。已有进程环境变量优先于 `.env`。

| 配置 | 用途 |
| --- | --- |
| `OTA_USERNAME` / `OTA_PASSWORD` | OTA 控制台账号及密码 |
| `OTA_P12_PATH` / `OTA_P12_PASSWORD` | 客户端 P12 文件及密码 |
| `OTA_API_TOKEN` | 对外 HTTP API 的 Bearer 令牌，至少 32 字符 |
| `OTA_CA_FILE` | 可选平台根 CA PEM；不填则显式信任 P12 内证书，适用于当前已验证部署 |
| `OTA_LOG_DIR` | 日志目录，默认 `./logs` |
| `OTA_STATE_DIR` | 本项目写操作锁及上传临时文件目录，默认 `./data` |
| `OTA_TIMEOUT` / `OTA_UPLOAD_TIMEOUT` | 普通 / 上传请求超时秒数，默认 60 / 1800 |
| `OTA_MAX_UPLOAD_BYTES` | 单包大小上限，默认 4 GiB |

不要关闭 TLS 校验。P12 在内存中解密；仅为加载 SSLContext 创建权限受限的临时 PEM，加载后立即移除，不持久保存会话 Cookie。

## HTTP API

本机启动：

```bash
uv run ota serve --host 127.0.0.1 --port 8000
```

供其他机器访问时改为 `--host 0.0.0.0`，客户端使用这台机器的可达地址。跨不可信网络部署时，在服务前配置 HTTPS 反向代理。所有 `/v1/` 接口要求 `Authorization: Bearer YOUR_API_TOKEN`。

- 交互文档：[Swagger UI](http://127.0.0.1:8000/docs)，点击 Authorize 填入令牌。
- OpenAPI：[接口定义](http://127.0.0.1:8000/openapi.json)。
- `/health` 仅检查进程存活，不连接 OTA 平台。

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| GET | `/v1/otas` | 查询；`dev_type`、`version`、`remarks`、`status=0/1`、`page`、`page_size` 等 |
| GET | `/v1/otas/{id}` | 单条任务；含 `remarks` 原文 |
| GET | `/v1/options` | 平台允许的设备、硬件、定制类型 |
| POST | `/v1/otas` | multipart 上传：`metadata` JSON 字符串和 `package` ZIP |
| DELETE | `/v1/otas/{id}` | 删除；支持 `?dry_run=true` |
| POST | `/v1/otas/batch-delete` | `{"task_ids":[...],"dry_run":false}` |
| GET | `/v1/otas/{id}/sns` | 读取 SN 规则 |
| POST | `/v1/otas/{id}/sns/add` | `{"entries":["SN或区间"],"dry_run":false}` |
| POST | `/v1/otas/{id}/sns/remove` | 同上；区间中的单个 SN 删除时自动拆分 |
| PUT | `/v1/otas/{id}/status` | `{"enabled":true,"push":false,"dry_run":false}` |
| POST | `/v1/otas/{id}/firmware` | `{"current_version":"设备当前版本","dvc_id":"可选设备SN"}` |

查询示例：

```bash
curl --get 'http://127.0.0.1:8000/v1/otas' \
  -H 'Authorization: Bearer YOUR_API_TOKEN' \
  --data-urlencode 'dev_type=SQ68' \
  --data-urlencode 'remarks=PATRICK'
```

添加 SN 和启用的请求形式如下。ID `24308` 仅用于展示，**这些请求会修改目标任务**，应先核对实时任务详情；`dry_run` 可预览而不写入：

```bash
curl 'http://127.0.0.1:8000/v1/otas/24308/sns/add' \
  -H 'Authorization: Bearer YOUR_API_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"entries":["801625490773333"],"dry_run":true}'

curl -X PUT 'http://127.0.0.1:8000/v1/otas/24308/status' \
  -H 'Authorization: Bearer YOUR_API_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"push":false,"dry_run":true}'
```

上传示例：修改 [examples/create-ota.json](examples/create-ota.json) 中的配置，再调用：

```bash
curl 'http://127.0.0.1:8000/v1/otas?dry_run=true' \
  -H 'Authorization: Bearer YOUR_API_TOKEN' \
  -F 'metadata=<examples/create-ota.json' \
  -F 'package=@/absolute/path/firmware.zip;type=application/zip'
```

常见返回：`403 operation_blocked` 表示备注或 SN 前置条件不满足；`409 state_conflict` 表示并发变更或重复配置；`409 write_result_unknown` 表示写入结果尚不能确认，需先查询核实；`422 invalid_input` 表示输入不合规；`503 audit_unavailable` 表示日志不能落盘。不要针对写接口的 409 / 502 / 503 自动重试。

## 命令行

CLI 与 HTTP API 使用同一执行层和删除限制：

```bash
uv run ota list --dev-type SQ68 --remarks PATRICK
uv run ota get 24308
uv run ota sns list 24308
uv run ota options

uv run ota sns add 24308 801625490773333 --dry-run
uv run ota sns remove 24308 00772633000003 --dry-run
uv run ota enable 24308 --dry-run
uv run ota disable 24308 --dry-run
uv run ota delete 24308 --dry-run
uv run ota create --metadata examples/create-ota.json --package /absolute/path/firmware.zip --dry-run
uv run ota firmware 24308 --current-version 13.25.0501.01
```

移除 `--dry-run` 才会提交对应写操作。`enable --push` 才明确请求额外推送。帮助：`uv run ota --help`。

`firmware` 示例中的当前版本必须替换成实际设备版本；接口会按平台匹配规则返回升级结果，不一定是指定任务。工具保留原始 JSON 并返回 `matched`，只有版本和 URL 中任务 ID 都对应时才标记匹配。禁用、SN 条件、当前版本及更新策略可能导致空结果或其他任务结果；不会为了查询固件修改任务状态。

## SDK

```python
from ota_agent import OTAClient, Settings

with OTAClient(Settings.from_env(), actor="my-integration") as ota:
    tasks = ota.list_otas({"devType": "SQ68", "remarks": "PATRICK"})
    task = ota.get_ota(24308)
    rules = ota.get_sns(24308)
    preview = ota.change_sns(24308, ["801625490773333"], dry_run=True)
```

每个线程或请求创建独立客户端，使用后退出上下文以登出和关闭连接。

## 删除限制与执行语义

删除执行层逐条检查当前 `remarks` 是否为字符串并含有字面量 `PATRICK`，**区分大小写**。`Patrick` 不满足要求，多行备注第一行有 `PATRICK` 可以满足。执行前再次读取全部目标；任一记录不符合、读取失败或不存在，就不提交批次。没有修改备注或 `force` 绕过的入口。

“批次整批拦截”指提交前本项目的检查；上游批量删除接口是否具有事务原子性未知。若上游执行了一部分，读回会返回结果不确定，而不会重放删除。备注检查与上游删除为两个请求，上游未提供条件删除，因此无法消除其他人在最后检查后立即改备注的竞争。

SN 采用读取、计算、复查、整份保存、读回核验。写锁协调本项目共用 `OTA_STATE_DIR` 的 CLI / API 进程；不限制原管理网页或其他部署的操作。删除最后一个 SN 会被拦截，因为平台空名单行为尚未验证；可以另行禁用任务。

新增会阻止相同设备配置、版本、语言及包类型重复上传。上传后唯一识别新任务并核验禁用状态；若平台默认启用，则先禁用这个新任务再返回成功。上传与禁用并非上游原子操作。无法唯一识别新记录时不更改候选任务并返回结果不确定。

## 落盘日志

日志默认保存到 `logs/audit-YYYY-MM-DD.jsonl`，时间为北京时间，单条记录是完整 JSON。文件权限 `0600`，使用跨进程文件锁及 `fsync`。写请求发出前会额外写入 `prepared` 记录，保留目标、SN 差异、删除前记录或上传文件 SHA-256；之后记录成功、失败及核验结果。API 返回 `X-Request-ID`，对应 `api.request` 日志，业务日志的 `actor=api:<请求ID>`。

密码、证书密码、Bearer 令牌、Cookie、私钥均不会写入日志；备注和 SN 属于审计数据，会被记录。日志写入失败时阻止后续请求；如果失败发生在已发出写请求之后，返回错误并要求读回核实。日志不是固件备份，也不是不可篡改的远程审计系统。

验证：

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

测试使用有状态模拟服务，不修改线上数据，覆盖 SN 区间、并发变化、超时结果核验、会话过期、上传、删除拦截、HTTP 鉴权及审计失败。已用真实证书验证新 SDK 的登录、查询、详情、SN 读取及候选值读取；新 SDK 的上传和删除流程尚未做线上写入验证。

初始调研见 [docs/ota-integration-plan.md](docs/ota-integration-plan.md)。
