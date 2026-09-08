# 收到数据但误报离线：修复与实机验收

## 结论与范围

2026-09-04（Asia/Shanghai），云端修复已部署，Windows 自包含 EXE 已更新并启动。实机从“数据更新超时”恢复为“在线 / 真实设备”，后续多个采集与心跳周期持续更新。此结论仅针对本次上传阻塞及离线误报，不代表无人值守部署的全部验收完成。

## 根因及修复

1. PostgreSQL 无法推断报警 ended_at 为空时的独立参数类型，报警批量接口返回 500。改为明确类型的 `to_timestamp(%s::double precision)`，保留事件幂等与租户校验。
2. 原采集器串行执行各类上传，一个报警失败会导致整个同步循环退避。配置、心跳、最新值、历史记录、报警现已独立工作及重试；单通道成功不会遮蔽其他通道错误。
3. 云端仅凭读数超过 45 秒便被前端解释为设备断线。新增 `sync_stale` 状态，保留最近读数并显示“更新超时 / 状态待确认”，不再错误显示“等待设备首次连接”。有明确设备错误时仍显示离线。

通用原则：采集成功、云端可达、数据新鲜、报警已同步是不同证据。适用于有本地缓存的采集系统；不能将补传的历史读数当作当前在线证明。例如设备断电后上传缓存，不应把设备恢复为在线。

## 分阶段验收

| 阶段 | 结果 / 证据 |
| --- | --- |
| 源码 | 修复 cloud_api.py、cloud_sync.py、public/app.js、public/i18n.js 和静态缓存版本；未修改协议解析、设备寄存器或报警阈值 |
| 自动化测试 | `python3 -m unittest discover -q`：132 项通过；包含 3 项 Node 状态渲染测试 |
| 数据库集成 | 独立 PostgreSQL 16 上实际调用报警入库：未结束、重复提交、结束更新通过，最终仅 1 行 |
| 故障隔离 | 阻塞并抛错的报警任务不阻塞最新值及心跳；单通道成功不会清除另一通道错误 |
| EXE 构建 | Windows x64 上使用应用目录内的官方 Python 构建；成品为自包含 EXE，不依赖系统 Python |
| 云端部署 | 镜像 `hawkhive-cloud:linkfix-20260904`，ID `677e4c9051c1910c40f8aca031e58e93cb68ac25a13b194c54f005685e646064`；仅更新 ingest-api，保留数据库及其他服务 |
| Windows 部署 | `C:\Users\mac\rdpc-cloud-link\output\collector-linkfix.exe --device --port 8789 --data-dir C:\Users\mac\rdpc-cloud-link\live-data --no-browser` |
| 真实链路 | 192.168.2.53 → 192.168.2.30:502 / Slave 1 → https://43.134.164.108；device 模式、source=device、设备 1/1 在线 |
| 上传 | 报警、历史、最新值及心跳均有生产 HTTP 200；本地原 3 条报警已全部标记成功同步，当前实例积压已清空 |
| 数据保护 | 原 live-data 与旧 EXE 保存在 Windows `rollback-linkfix-20260904-231334`；现有配置、令牌与实例 ID 沿用；SQLite quick_check=ok |
| 发布边界 | 未推送 Git、未发布 Docker Hub、未生成新的安装服务包；已部署到当前云端和指定 Windows 测试机 |

EXE SHA-256：`d735fc6bfd6f3d4f5e960c55dd0bf828c6ed07e846c4d656361262f3718275fe`。

## 前端 QA

环境：https://43.134.164.108/，已登录真实客户账号，Chrome 桌面约 1497×686，Browser 可用，无降级。使用 frontend-testing-debugging 的渲染验收流程，补充状态回归与实际浏览器操作，而不只检查构建成功。

目标流程：实时页加载 → 超时状态 → Windows 更新恢复上传 → 在线真实数据 → 历史页 → 返回实时页 → 中英文切换。

| 检查 | 结果 |
| --- | --- |
| 页面身份 | URL / HawkHive 标题符合目标 |
| 非空白 | 设备、温湿度、颗粒读数、趋势正常渲染 |
| 框架错误遮罩 | 未出现 |
| Console | 本次验收捕获的 warn/error 列表为空 |
| 截图 | `dist/linkfix-20260904/cloud-online.png` 显示“在线”、真实数据及数据新鲜度 |
| 交互 | 实时→历史→实时及中英文切换成功，历史 API 返回 200 |

关键验证命令 / API：unittest、Node runtime assertions、实际 PostgreSQL ingest_alarms、PyInstaller、Docker Compose 单服务部署、Browser reload / AX state / screenshot / dev.logs。

## 尚未解决及使用注意

- 独立的本地自动备份存在 WinError 32 文件占用错误，本次未修复。数据库 quick_check 通过且有升级前冷备份，但不能将其视为自动备份已正常。
- 当前是便携 EXE 前台运行，窗口不能关闭；未改造开机自启服务。原 HawkHiveCollector 服务保持停止，旧服务数据未动；原服务启动类型未改，重启后的版本/实例接管仍需单独验收。
- 没有故意断开生产设备或向设备写寄存器；断线、恢复与失败隔离通过隔离测试覆盖，实机仅做正常采集上传验证。
- 未做长时间稳定性、Windows 重启、多设备负载、移动端及其他浏览器验收。
- 不要无参数双击本成品进行真实采集：现有启动器的无参数默认是演示模式，必须使用 `--device` 并指定原数据目录。

## 回滚

云端保留镜像 `hawkhive-cloud:before-linkfix-20260904` 与 `/opt/hawkhive/linkfix-backup-20260904/`。Windows 保留旧 EXE、原数据冷备份；回滚时先停止新实例，复用当前 live-data，不能直接覆盖较新的业务数据。
