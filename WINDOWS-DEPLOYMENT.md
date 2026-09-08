# Windows 现场采集器部署说明

版本：2026-09-03  
当前交付入口是 PyInstaller 单文件 EXE。自动接入版 EXE 内含 Python 运行时，目标电脑不需要 Python；首次双击会申请管理员权限、复制到机器级目录并注册 SYSTEM 开机任务。同一客户下载的 ZIP 可在该客户多台电脑重复使用；每台电脑自动生成独立身份并出现在云端待加入列表，管理员决定加入并按需修改名称后才取得独立上传凭据，不需要抄录配对码。下文第 2–7 节的 Python/SCM 服务流程只保留为旧试点维护参考，不得作为新版现场安装说明。代码签名、正式卸载器、目标 Windows 版本矩阵和断电恢复仍属于量产发布门槛，不能仅凭本地构建判定完成。多车间自动接入详见 `AUTOMATED-ONBOARDING.md`。

## 1. 设计依据

- 使用 Windows 服务承载采集进程，设为延迟自动启动；服务在无人登录时也能运行。Microsoft 对服务启动类型的定义见 [sc.exe create](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/sc-create)。
- 运行数据放在 `%ProgramData%\HawkHive\DCP8001`。Microsoft 将 `FOLDERID_ProgramData` 定义为机器共享的固定应用数据目录，默认位于 `%SystemDrive%\ProgramData`，见 [KNOWNFOLDERID](https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid)。
- 云端令牌使用 Windows DPAPI 的机器范围加密，配置文件目录只授权服务账号、LocalSystem 与本机管理员。Microsoft 明确说明机器范围密文可被同一电脑上的其他用户解密，因此目录 ACL 是必要的第二道边界，见 [CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)。
- Python 服务封装使用 pywin32 的 `ServiceFramework` 和 `HandleCommandLine`，实现依据见 [pywin32 服务工具源码](https://github.com/mhammond/pywin32/blob/main/win32/Lib/win32serviceutil.py)。

## 2. 旧版 Python/SCM 试点安装前提（非当前交付流程）

- Windows 10、Windows 11 或 Windows Server 2019 及以上，具体客户版本仍需实机确认。
- 64 位 Python 3.10 或以上，使用系统级安装；不要依赖某个登录用户的临时虚拟环境。
- 安装账号具有本机管理员权限。
- 发布包已解压到 `C:\Program Files\HawkHive\DCP8001Collector`；不要从下载目录、桌面或某个用户的虚拟环境直接注册服务。
- Windows 不休眠，设备网卡与互联网出口网卡均保持启用。
- 现场防火墙允许采集器访问设备 TCP `502`，允许出站 HTTPS `443`。

## 3. 旧版 Python/SCM 安装（非当前交付流程）

将发布包解压到 `C:\Program Files\HawkHive\DCP8001Collector`。以管理员身份打开 PowerShell，在该目录运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install-windows-service.ps1
```

脚本会执行以下动作：

1. 创建 `%ProgramData%\HawkHive\DCP8001`。
2. 将该目录 ACL 收紧为 LocalSystem/本机管理员完全控制，并仅授予低权限 LocalService 修改权。
3. 以系统级 Python 安装锁定版本的 `pyserial`、`psutil` 与 `pywin32`，并执行 pywin32 官方要求的机器级服务组件注册。
4. 使用低权限 `NT AUTHORITY\LocalService` 注册 `HawkHiveDCP8001Collector` 服务，设置延迟自动启动和三次失败重启。
5. 启动服务。

重复运行安装脚本时会停止并更新现有服务，不会覆盖 `%ProgramData%` 中的数据库和配置。量产自动升级与版本回滚仍是未完成门槛。

安装后在本机浏览器打开：

```text
http://127.0.0.1:8787
```

服务只监听 `127.0.0.1`，不要改成 `0.0.0.0` 暴露到车间网络。办公室和值班人员应访问云端平台，而不是直接访问现场电脑的本机控制台。

## 4. 运行数据

默认目录：

```text
C:\ProgramData\HawkHive\DCP8001
├── dashboard_data.sqlite3
├── collector_settings.json
├── collector-enrollment-secret.json（仅待确认期间存在）
├── collector-enrollment-status.json（仅待确认期间存在）
├── collector.lock
├── backups\
└── logs\
```

- `dashboard_data.sqlite3` 保存读数、报警、设备清单和上传队列。
- `collector_settings.json` 不保存明文令牌；Windows 下保存 DPAPI 密文。
- `backups` 每个 UTC 自然日生成一次 SQLite 在线备份，默认保留 7 份。
- 默认只清理 90 天前且已经获得云端确认的读数；未上传和隔离记录不会被自动删除。
- 环境变量 `DCP_LOCAL_RETENTION_DAYS` 可将本机已上传数据保留期设为 7–3650 天。
- 如需更换数据盘，可在系统级设置 `DCP_DATA_DIR`，迁移必须在停止服务并完成备份后进行。

从旧版程序目录升级时，如果新数据目录尚无数据库或配置，启动器会通过 SQLite 在线备份方式复制旧数据库，并保留旧文件作为回退；不会覆盖已存在的新数据。

## 5. 多网卡与设备发现

- 本机控制台会列出所有启用的私有 IPv4 网卡，而不是只选择通往云端的网卡。
- 为避免对大网段产生不可控扫描，单次最多扫描 254 个可用地址。
- `/16`、`/20` 等大网段只建议当前 IP 所在的 `/24`；其他子网需按客户授权的 IP 规划逐段扫描。
- 候选设备必须同时读取实时寄存器 `0` 和固件寄存器 `36` 才标记“已读取目标协议”。由于厂家资料未提供型号/序列号寄存器，这仍不等于型号身份已验证。
- 正式启用采集前还会读取单位寄存器 `133`；当前只接受值 `1`（`PCS/28.3L`）。如果设备是其他单位，必须先统一现场配置或取得厂家确认，软件不会静默换算。

## 6. 服务操作与诊断

```powershell
Get-Service HawkHiveDCP8001Collector
Restart-Service HawkHiveDCP8001Collector
Get-WinEvent -LogName Application -MaxEvents 100 |
  Where-Object ProviderName -Match "HawkHiveDCP8001Collector|Python Service"
```

本机健康接口：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/health
```

本机控制台会显示设备、采集、云端、上传队列和存储五类状态。磁盘不足 10 GiB（或同时低于 10% 与 20 GiB）时预警；不足 1 GiB（或同时低于 3% 与 5 GiB）时提示严重。联合使用绝对容量与比例，避免大磁盘误报。严重状态不会删除未上传记录，现场必须及时扩容或处理已经上传的数据。

## 7. 卸载

```powershell
.\uninstall-windows-service.ps1
```

卸载脚本只删除服务注册，不删除 `%ProgramData%\HawkHive\DCP8001`，以便恢复数据。确认无需恢复后，数据删除应作为单独、人工确认的运维动作。

## 8. 现场验收清单

- [ ] 重启 Windows 且无人登录，服务自动进入 Running。
- [ ] 浏览器关闭后，数据库读数仍持续增加。
- [ ] 双网卡环境中能明确选择设备所在网卡。
- [ ] 扫描结果不会把“仅端口开放”显示为已验证设备。
- [ ] 每台设备的单位寄存器 `133` 为 `1`，连接测试显示 `PCS/28.3L`；修改为其他单位时采集明确失败且不产生错误单位数据。
- [ ] 拔掉互联网 30 分钟，本机继续采集，待上传数量增加。
- [ ] 恢复互联网后，积压清零，云端无重复记录。
- [ ] 停止一台设备不影响其他设备采集；恢复后自动上线。
- [ ] 非正常断电后数据库 `quick_check` 为 `ok`，服务自动恢复。
- [ ] 令牌文件中没有明文令牌，普通 Windows 用户不能读取数据目录。
- [ ] 卸载服务后数据目录仍存在；重新安装能恢复原数据。

## 9. 当前未完成的量产门槛

- 尚未在真实 Windows 10/11/Server 主机执行服务安装与开机恢复测试。
- 尚未制作代码签名的 EXE/MSI，也未建立签名证书、自动升级和失败回滚链路。
- 本机控制台当前依赖“回环地址 + Host/Origin + 请求令牌”保护；如果现场电脑不是专用电脑，还必须增加本机管理员登录，不能只依赖回环地址。
- 厂家尚未提供设备唯一身份字段，无法阻止同网段其他兼容 Modbus 设备被误选。
- 真实设备、真实客户网络与正式云端的连续断网补传测试尚未执行。

## 10. 防重复启动及本地备份（2026-09-05）

新版在 Windows 上使用整机级命名互斥量，便携入口和源码服务入口共用启动保护；不同数据目录、不同端口、重命名 EXE 也不能同时运行第二套新版采集器。启动检查发生在迁移/初始化数据库和启动采集之前。同数据目录仍保留文件锁作为额外保护。

- 重复启动会显示“采集器已在运行，不能重复启动”，第二个实例退出，原实例继续工作。无界面启动返回退出码 2。
- 进程异常退出后 Windows 会回收互斥量；不要删除 collector.lock 或强行解锁来启动第二套程序。
- PyInstaller 单文件 EXE 通常有引导父进程和实际工作子进程，任务管理器看见两个同名进程不等于两套采集器。应结合实例锁及实际采集线程判断。
- SQLite 备份连接在改名之前显式关闭，每次使用独立临时文件，并校验备份完整性；维护操作另有跨进程锁。不会删除其他尝试遗留的临时文件。
- 成功恢复后，历史失败仍保留在日志中，但不再显示为当前故障；当前仍失败或未验证恢复时继续报警。
- 此保护不是对任意旧软件或第三方采集软件的设备访问隔离；旧版本不认识新互斥量。例如另一台电脑运行厂家软件仍可能争抢设备，应纳入现场管理。

当前实机使用桌面 `HawkHive Collector (Live)` 入口，带真实采集参数及原 live-data 路径。旧 `HawkHiveCollector` 服务经用户确认已禁用，旧程序及数据保留；两个已知便携旧入口同步更新。当前仍是前台便携部署，窗口不能关闭，尚未宣称完成无人登录自启与整机重启验收。
