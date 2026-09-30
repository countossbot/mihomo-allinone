# mihomo-allinone

基于**官方 [metacubexd-server](https://github.com/MetaCubeX/metacubexd)** 的一体化部署配置。

单容器内含 **mihomo 内核 + metacubexd 面板**，面板自带订阅导入、节点管理、配置编辑等完整功能。

> 本仓库只提供部署编排（compose + 配置模板），不含任何自研代码，也不含订阅、节点等敏感数据。

---

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/countossbot/mihomo-allinone.git
cd mihomo-allinone

# 2. 生成两个密钥并写入 .env
cp .env.example .env
sed -i '' "s/^CONTROL_TOKEN=.*/CONTROL_TOKEN=$(openssl rand -hex 16)/" .env
sed -i '' "s/^CLASH_SECRET=.*/CLASH_SECRET=$(openssl rand -hex 16)/" .env

# 3. 启动
docker compose up -d
```

---

## 端口

| 端口 | 用途 |
|---|---|
| `8080` | **面板**（浏览器访问，含配置管理页） |
| `9090` | 内核控制 API（面板内部使用，不对外） |
| `7890` | 混合代理口（HTTP + SOCKS5） |

默认全部只绑 `127.0.0.1`。若需公网访问，在外层加反向代理（见下文）。

---

## 使用

### 1. 打开面板

```
http://127.0.0.1:8080
```

用 `.env` 里的 `CONTROL_TOKEN` 登录。

### 2. 导入订阅

面板左侧 **配置（Profiles）** → 新建 → 选择「订阅」→ 填入订阅链接 → 保存并激活。

> 面板会把配置渲染到 `/data/active.yaml`，内核按该文件运行。

### 3. 客户端连接

代理口 `127.0.0.1:7890`，支持 HTTP 与 SOCKS5：

```bash
curl -x http://127.0.0.1:7890 https://ipinfo.io/ip
curl --socks5-hostname 127.0.0.1:7890 https://ipinfo.io/ip
```

---

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `CONTROL_TOKEN` | ✅ | 面板登录令牌 |
| `CLASH_SECRET` | ✅ | 内核 API 密钥（须与上面不同） |
| `DEFAULT_BACKEND_URL` | | 面板预填的后端地址，默认 `http://127.0.0.1:9090` |
| `TZ` | | 时区，默认 `Asia/Shanghai` |

生成密钥：
```bash
openssl rand -hex 16
```

---

## 数据持久化

挂载 `./data` 到容器 `/data`，保留：

```
data/active.yaml     内核运行配置（面板生成）
data/profiles/       订阅配置
data/providers/      订阅缓存
data/cache.db        面板状态
data/geoip.dat       地理数据（首次启动自动下载）
data/geoip.metadb
data/geosite.dat
```

---

## 公网访问（反向代理）

若要让面板走域名 + HTTPS，推荐用 Caddy。示例（Cloudflare Flexible 场景）：

```caddyfile
http://panel.example.com {
    reverse_proxy 127.0.0.1:8080 {
        # 面板含 SSE 实时日志流，需关闭响应缓冲
        flush_interval -1
    }
}
```

⚠️ **务必给面板加访问控制**（Basic Auth 或 Cloudflare Access），否则公网可直连面板。

---

## 已知行为

| 现象 | 说明 |
|---|---|
| 改了配置但不生效 | 面板的「保存」只写 `profiles/`，需再点一次**「激活」**才会重新渲染 `active.yaml` |
| 删了配置但节点还在 | 删除 profile 不会清理 `active.yaml`，需**重启内核**才回落 |
| 导入订阅报 500 | 面板拉订阅时固定用 `User-Agent: clash.meta`，部分机场会按 UA 过滤 |

---

## 更新镜像

```bash
docker compose pull
docker compose up -d
```

---

## 卸载

```bash
docker compose down -v      # -v 会同时删除数据卷
```

---

## 致谢

- [mihomo](https://github.com/MetaCubeX/mihomo) —— 内核
- [metacubexd](https://github.com/MetaCubeX/metacubexd) —— 面板
