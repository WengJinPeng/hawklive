# avi.hawk-hive.com 服务器配置与验收

2026-09-08（Asia/Shanghai）。已在 43.134.164.108 的现有 Caddy 中增加 https://avi.hawk-hive.com，保留原 HTTPS IP 入口。

## 变更

- 原问题：Cloudflare 返回 525；源站 Caddy 仅有 IP 站点，没有新域名证书和路由。
- 活动配置：服务器 /opt/hawkhive/Caddyfile.ip，新增域名站点，反代同一 ingest-api:8000。
- 使用 Let's Encrypt 正式 ACME 自动管理证书；未修改 Cloudflare 安全设置。
- 证书签发者 Let's Encrypt YE1；有效期 2026-09-08 00:48:35 UTC 至 2026-12-07 00:48:34 UTC。已配置自动续期，未来续期执行尚待实际发生。
- 原配置备份：服务器 /opt/hawkhive/backups/config/avi-domain-20260908T014603Z/Caddyfile.ip。
- 经 caddy validate 后执行热加载；未重建或重启 API，未修改数据库、采集器令牌或设备分配。
- API 仍为容器 3392e13ed58901af54e5dc77141ad3eac93f80cae6fed9e2107fcb30bedf9e03，镜像 sha256:677e4c9051c1910c40f8aca031e58e93cb68ac25a13b194c54f005685e646064，启动时间 2026-09-04T15:10:34.370890644Z，healthy。

## 验证

- 经 Cloudflare 的新域名首页、/api/v1/health 均 HTTP 200，健康响应 {"ok":true}。
- 使用 --resolve 直连源站、携带新域名 SNI：证书校验成功，健康接口 HTTP 200。
- 源站本机通过 Python 标准证书信任库验证 avi.hawk-hive.com 成功，SAN 匹配。
- HTTP 请求 308 到相同路径的 HTTPS 地址。
- 原 https://43.134.164.108/api/v1/health 保持 HTTP 200。
- 新域名浏览器通过原客户账号正常登录，显示 Addvalue、真实设备来源及新鲜读数；设备页面可打开。
- 09:48 左右页面显示采集器心跳 2/2：第一台心跳约 7 秒前，第二台约 23 秒前；两个设备有新鲜读数。仍有部分设备 timed out、第二台本地存储 warning（可用约 11 GB），未作为域名故障处理或修改。
- 此次未把现场采集器地址切换到新域名。网页看到的实时数据证明云端继续收数，不代表已验证现场电脑通过新域名上传。

## 回滚与后续使用

在服务器将备份内容写回原活动文件（保留绑定挂载文件 inode），执行：

    docker exec hawkhive-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile

现场采集器如改用新域名，仅将 Cloud URL 改为 https://avi.hawk-hive.com，同一节点的 Site ID 和 Edge Token 沿用，随后验证该电脑连接测试、云端心跳与真实数据。

依据：[Caddy 站点配置](https://caddyserver.com/docs/caddyfile/concepts)。
