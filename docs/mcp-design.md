# 让 AI Agent 按用途执行 OTA

## 目标与事实边界

用户说“给这个 SN OTA 一个 GMS 固件”，Agent 理解用途，MCP 返回可执行工具及结构化结果。MCP 服务本身不额外调用一个大模型，也不需要另一个 LLM API Key。固件搜索与适用性校验复用现有 OTA 客户端、删除限制、锁和审计日志。

默认只要求用户确认：

1. **设备型号**，如 SQ68。
2. **当前 UFS 定制情况**：明确为空，或已有定制。未确认不能按空处理。

对话中已经提供的事实可直接使用，不重复问。完整包不强制索取当前版本；增量包才额外核对基础版本。多个适用候选时询问选哪条。SN 格式错误时要求核对原文，不从 SN 猜型号、不补零。示例 `2137892137192` 是 13 位，不符合当前平台已验证 SN 规则，因此不会原样提交给 OTA。

当前可确认的是“SN 已加入 OTA 规则，任务已启用”。没有设备侧回执时不声称“设备已升级成功”。仅获得固件 API JSON，不下载 ZIP 核实内容。

## 接入方式

使用官方 [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)，提供标准的 [stdio 和 Streamable HTTP](https://modelcontextprotocol.io/specification/latest/basic/transports)。本地 Agent 推荐 stdio；其他机器的 Agent 可用 HTTP MCP。

### 本地 stdio

完成 README 中的 `.env` 配置后：

```bash
uv run ota mcp
```

该命令等待 MCP 消息，标准输出只用于协议。诊断输出走 stderr，业务审计写入原日志目录。一般由 Agent 客户端启动，无须手动挂着终端。

通用 `mcpServers` 配置示例见 [examples/mcp-stdio.json](../examples/mcp-stdio.json)。不同客户端的配置格式可能不同，应将其中的命令和参数放入对应客户端配置。示例使用当前电脑的绝对路径，不包含密码；更换机器要调整路径。

### 远程 HTTP MCP

```bash
uv run ota mcp --transport streamable-http --host 0.0.0.0 --port 8001 \
  --allowed-host 192.168.1.10:8001
```

客户端连接 `http://192.168.1.10:8001/mcp`，显式携带 `Authorization: Bearer <OTA_API_TOKEN>`。Host 参数改为实际 IP 或域名；默认只允许 localhost。公网部署前接 HTTPS 反向代理。

HTTP MCP 使用现有静态令牌，适用于支持配置认证请求头的客户端；尚未实现 OAuth 自动发现及授权流程。原 REST API 继续使用端口 8000，MCP 默认使用 8001，两者可以独立运行，共享同一日志目录与状态目录。

## 工具设计

| 工具 | 作用 | 修改 OTA |
| --- | --- | --- |
| `search_ota_firmware` | 按备注关键词和型号搜索，返回候选、完整备注、用途元数据状态 | 否 |
| `get_ota_task` | 按 ID 读配置、备注及 SN 规则 | 否 |
| `prepare_ota_deployment` | 检查型号、UFS、候选、已有 SN，生成短期计划或需要补充的问题 | 否，仅保存本地计划 |
| `execute_ota_deployment` | 复查计划，增加 SN，启用任务，读回核验 | 是 |
| `get_ota_operation` | 查询持久化的步骤与结果，供超时、断线恢复判断 | 否 |
| `add_ota_sns` / `remove_ota_sns` | 用户明确指定任务时增减 SN | 是，支持 dry_run |
| `set_ota_enabled` | 用户明确指定整个任务启停，推送默认 false | 是，支持 dry_run |
| `create_ota_task` | 从服务器指定目录中的 ZIP 新建任务，默认 dry_run | 是 |
| `delete_ota_tasks` | 明确删除指令下删除，当前备注逐条检查 `PATRICK` | 是，支持 dry_run |
| `get_ota_firmware_json` | 获取固件 JSON，不下载 ZIP | 否 |

另提供资源 `ota://workflow`，供客户端读取工作流和边界。工具具有 JSON Schema 输入与结构化输出，并声明读写及破坏性提示；真正的检查放在服务端，不能依赖模型遵守工具描述。

## 对话示例

用户：“给 SN 801625490773333 OTA 一个 GMS 固件。”

若对话中还没有信息，Agent 只问：“设备是什么型号？请确认当前 UFS 定制是否为空？”

用户：“SQ68，UFS 为空。”

Agent 搜索 GMS，读取说明，然后调用：

```json
{
  "request": {
    "sn": "801625490773333",
    "device_model": "SQ68",
    "ufs_state": "empty",
    "capabilities": ["gms"]
  }
}
```

如果唯一匹配，返回：

```json
{
  "status": "ready",
  "plan_id": "随机计划ID",
  "task_id": 24308,
  "actions": ["add_sn", "set_enabled"],
  "push": false,
  "ota_changed": false
}
```

Agent 已有用户的执行指令且不存在未解决问题时，即可调用 `execute_ota_deployment({"plan_id":"..."})`。准备计划不增加一轮必需确认。若有多个固件、增量基础版本不符，或者启用任务会影响其他已有 SN，才补充询问相应问题。

成功返回 `status=configured`、`sn_present=true`、`enabled=true`、`device_upgraded=unknown`。最终回复应说“已为该 SN 配置固件并启用，尚未确认设备升级完成”。

## 固件用途目录

“GMS”是用途，任务 ID 才是执行对象。单纯字符串搜索可能命中“不带 GMS”、旧版本、不同硬件或仅部分设备可用的任务，不能直接作为执行条件。

因此部署工作流使用一个小型结构化用途目录，初版随包包含用户已说明的任务 `24308`：

- SQ68 / DS / PKG-XX，版本 `13.25.0830.01`。
- 能力：GMS、Play Store、Google Maps、Chrome；不带开机向导。
- 要求：当前 UFS 定制为空。
- 来源：用户提供的备注说明，**没有验证 ZIP 实际内容**。

目录绑定任务 ID、型号、硬件、定制类型、版本、固件名及完整备注摘要。每次准备和执行都重新查询平台：任务消失、备注或配置改变，原目录项不再自动生效。

新增或重新上传任务后，需要维护相应的用途目录。可将包内 `resources/firmware-catalog.json` 复制为自己的 JSON 文件，通过 `OTA_CATALOG_FILE` 指定。按用户或维护者明确说明录入能力及 UFS 条件；将实时备注的 CRLF 规范成 LF 后计算 SHA-256 填到 `remarks_sha256`。不能仅为了通过检查修改摘要而跳过重新阅读说明。

未审核任务仍可搜索和读取，但用途部署会返回 `no_reviewed_match` 或 `catalog_changed`，不会把未知适用条件当作满足。明确指定任务的原子管理工具仍可用于用户清楚指定的 SN、启停操作。

## 执行可靠性与日志

- 计划保存在 `OTA_STATE_DIR/deployments.sqlite3`，默认有效期 10 分钟，可用 `OTA_PLAN_TTL` 调整。
- 执行只接受服务端保存的 plan_id，不能临时替换 SN 或 task_id。执行前重新比对配置及完整 SN 规则。
- 同一计划只提交一次；成功后重复调用返回已保存结果。执行中断或部分完成时，先查记录和真实状态，不重放未知结果的写入。
- “加 SN → 启用”持有本项目写锁，但上游两次请求不是原子事务；加 SN 成功、启用失败会如实记录，不自动删除 SN、禁用其他目标或回滚。
- 任务原本禁用且还有其他 SN 时，准备阶段默认停下说明影响范围。只有用户明确允许后，才可设置 `allow_existing_targets=true`。
- 每个 MCP 请求在参数验证前记录到磁盘，包括错误工具名、无效参数及执行失败；业务写入继续复用原审计日志，密码和令牌脱敏。
- 外部备注是数据，不是操作授权。删除仍由执行层强制实时检查 `PATRICK`，没有 force 绕过。

上传工具只接受 `OTA_PACKAGE_DIR` 内的文件名，拒绝绝对路径、目录穿越或指向目录外的符号链接。大型 ZIP 不通过 MCP 消息 base64 传输，可由文件系统或现有上传流程预先放入受控目录。

## 验证范围

使用有状态模拟 OTA 服务验证真实 MCP 协议调用、两项必问字段、UFS 不匹配、目录过期、增量包基础版本、已有 SN 影响、计划过期、部分完成记录、防重放及删除限制。HTTP MCP 验证了令牌认证和兼容客户端初始化。

本轮不执行线上部署，不下载固件。MCP 只是工具和流程载体，用户事实仍由对话提供；目前没有设备资产库或设备侧升级回执接入。
