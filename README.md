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

# 2. 生成密钥并写入 .env（两个密钥用同一个值，省得记混）
cp .env.example .env
SECRET=$(openssl rand -hex 16)
sed -i '' "s/^CONTROL_TOKEN=.*/CONTROL_TOKEN=$SECRET/" .env
sed -i '' "s/^CLASH_SECRET=.*/CLASH_SECRET=$SECRET/" .env

# 3. 启动
docker compose up -d
```

---

## 端口

容器内端口固定；**宿主机端口可在 `.env` 覆盖**，默认值已避开常见占用：

| 容器内 | 宿主机默认 | 用途 |
|---|---|---|
| `8080` | **`18080`** | **面板**（浏览器访问，含配置管理页） |
| `9090` | **`19090`** | 内核控制 API（面板连接用） |
| `7890` | **不映射** | 混合代理口（HTTP + SOCKS5） |

> **代理口默认不映射到宿主机**，避免与本机已有代理（如 ClashX 占用 7890）冲突。
> 需要从宿主机连代理时，在 `docker-compose.yml` 里取消该行注释。

所有端口默认只绑 `127.0.0.1`。若需公网访问，在外层加反向代理（见下文）。

---

## 使用

### 1. 打开面板

```
http://127.0.0.1:18080
```

登录时填 `.env` 里的 `CONTROL_TOKEN`。

> **两个密钥的区别**（容易混）：
> - `CONTROL_TOKEN` —— 登录**面板 UI**
> - `CLASH_SECRET` —— 面板**连接内核 API**（登录页那个「密钥」框填这个）
>
> 为省事可以把两者设为**同一个值**。

### 2. 导入订阅

面板左侧 **配置（Profiles）** → 新建 → 选择「订阅」→ 填入订阅链接 → 保存并**激活**。

> 面板会把配置渲染到 `/data/active.yaml`，内核按该文件运行。
> **改了配置务必点一次「激活」**，否则不会生效。

### 3. 客户端连接

代理口默认**不映射到宿主机**。需要时，在 `docker-compose.yml` 里取消该行注释再重启：

```yaml
- "127.0.0.1:17890:7890"
```

然后使用 HTTP 或 SOCKS5：

```bash
curl -x http://127.0.0.1:17890 https://ipinfo.io/ip
curl --socks5-hostname 127.0.0.1:17890 https://ipinfo.io/ip
```

---

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `CONTROL_TOKEN` | ✅ | 无 | 登录面板 UI 用 |
| `CLASH_SECRET` | ✅ | 无 | 面板连接内核 API 用（可与上面同值） |
| `DEFAULT_BACKEND_URL` | | `http://127.0.0.1:19090` | 面板预填给**浏览器**的后端地址 |
| `PANEL_PORT` | | `18080` | 宿主机上的面板端口 |
| `API_PORT` | | `19090` | 宿主机上的 API 端口 |
| `PROXY_PORT` | | `17890` | 代理口端口（需取消 compose 里对应行注释） |
| `TZ` | | `Asia/Shanghai` | 时区 |

> ⚠️ `DEFAULT_BACKEND_URL` 是**浏览器视角**的地址，不是容器内地址。
> 本地部署填宿主机映射的 API 端口；经反向代理则填反代路径（如 `/mihomo-api`）。

生成密钥：
```bash
openssl rand -hex 16
```



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
    reverse_proxy 127.0.0.1:18080 {
        # 面板含 SSE 实时日志流，需关闭响应缓冲
        flush_interval -1
    }
}
```

同时把 `.env` 里的后端地址改为反代路径（面板会用它作为预填值）：

```bash
DEFAULT_BACKEND_URL=/mihomo-api
```

并在 Caddy 里把该路径反代到内核 API：

```caddyfile
handle /mihomo-api/* {
    uri strip_prefix /mihomo-api
    reverse_proxy 127.0.0.1:19090 { flush_interval -1 }
}
```

⚠️ **务必给面板加访问控制**（Basic Auth 或 Cloudflare Access），否则公网可直连面板。

---

## 已知行为

| 现象 | 说明 |
|---|---|
| **登录页提示「后端不可达」** | 登录页那个「密钥」框要填 `CLASH_SECRET`（内核密钥），不是 `CONTROL_TOKEN`（面板令牌）。两者设为同值可避免混淆 |
| **面板打不开 / 本机断网** | 代理口 `7890` 若映射到宿主机，会与本机已有代理（如 ClashX）冲突；系统代理可能被指向空配置的容器。默认已不映射该端口 |
| 改了配置但不生效 | 面板的「保存」只写 `profiles/`，需再点一次**「激活」**才会重新渲染 `active.yaml` |
| 删了配置但节点还在 | 删除 profile 不会清理 `active.yaml`，需**重启内核**才回落 |
| 导入订阅报 500 | 面板拉订阅时固定用 `User-Agent: clash.meta`，部分机场会按 UA 过滤 |
| 首次打开显示旧状态 | 面板把连接信息存在浏览器 localStorage，优先于 `config.js`。异常时**硬刷新**（Cmd/Ctrl+Shift+R）或点「切换后端」 |

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
