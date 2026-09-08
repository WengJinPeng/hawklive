# 公网 IP + HTTPS Docker 部署

适用于具有固定公网 IPv4 的 Linux 服务器，不依赖 sslip.io、DuckDNS 或其他域名。
当前部署入口为 `https://43.134.164.108`；迁移时请替换成新服务器的公网 IP。

## 配置与启动

1. 安装 Docker Engine 和 Compose 插件，将不含运行数据、密码的源码包解压到
   `/opt/hawkhive`。
2. 在云安全组和主机防火墙允许 TCP 80、443 入站；SSH 22 尽可能仅限管理员 IP。
   不要向公网开放数据库 5432、应用 8000 或设备 Modbus 502。
3. 首次部署复制 `.env.example` 为 `.env`，填写随机生成的强数据库密码，并设置：

   ```dotenv
   DASHBOARD_DOMAIN=43.134.164.108
   DCP_CADDYFILE=./Caddyfile.ip
   DASHBOARD_SECURE_COOKIE=1
   ```

   `DASHBOARD_DOMAIN` 只填 IP，不加协议、端口或路径。生产环境不要设置测试 CA。
   已有部署只编辑这些相关项，保留原数据库密码和所有数据卷。
4. 启动并检查：

   ```bash
   cd /opt/hawkhive
   chmod 600 .env
   sudo bash cloud-ops.sh start
   sudo bash cloud-ops.sh status
   curl --fail --show-error https://43.134.164.108/api/v1/health
   ```

首次启动会构建应用镜像，并由 Caddy 自动申请正式 IP 证书。健康接口成功不代表
设备已经上传数据；还需验证登录、现场采集器连接、最新数据时间和历史记录。
首次客户账号与采集器 token 的创建流程见 `DEPLOY-ALIYUN.md` 第 6 节。

## 证书与续期

- Compose 使用 Caddy 2.11.4，`Caddyfile.ip` 明确选择 Let's Encrypt 的
  `shortlived` profile，而非自签名证书。
- IP 证书有效期为 160 小时。Caddy 根据 CA 的续期信息自动续期，无需手动替换
  证书文件；保持容器运行、服务器时间准确、80/443 可从公网访问，并允许访问 CA。
- `default_sni` 为不发送 SNI 的 IP 客户端选择正确证书，不能省略。
- `caddy_data` 数据卷持久保存 ACME 账号、证书和私钥。不要删除它，也不要执行
  `docker compose down -v`，后者还会删除数据库数据卷。
- 实际续期是否成功要查看新证书到期日和 Caddy 日志，不能只凭容器运行状态判断。

```bash
sudo docker compose -f docker-compose.cloud.yml logs --tail 100 caddy
openssl s_client -connect 43.134.164.108:443 -noservername \
  -verify_ip 43.134.164.108 -verify_return_error </dev/null
```

排障时可临时设置
`DCP_ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory`，但测试 CA
证书不会被浏览器信任。验证挑战通过后删除此设置，重新创建 Caddy 容器，并用
不带 `-k` 的 curl 验证正式证书。若只改了 Caddyfile 或 `.env`，运行：

```bash
sudo docker compose -f docker-compose.cloud.yml up -d --force-recreate caddy
```

官方说明：[Let's Encrypt IP 证书](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability.html)、
[Caddy default_sni](https://caddyserver.com/docs/caddyfile/options#default-sni)、
[Caddy ACME profile](https://caddyserver.com/docs/caddyfile/directives/tls)。

## 现场采集器

Mac 或 Windows 采集器继续在设备所在局域网读取 `192.168.2.30:502`，云服务器
不会直接访问现场私网设备。将采集器的云地址设为 `https://43.134.164.108`，
填写对应站点 ID 与专用上传 token，保持 TLS 证书校验开启。

公网 IP 证书解决地址和加密问题，不保证所有运营商线路都能连通。固定 IP 更换、
防火墙关闭或续期失败都可能造成访问中断；动态公网 IP、私网 IP 不适用此配置。
例如 `192.168.2.30` 不能照此申请公网信任证书。
