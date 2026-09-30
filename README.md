# mihomo-allinone

单容器整合：**mihomo 内核 + metacubexd 面板 + 订阅管理/刷新服务**。

多架构镜像（linux/amd64 + linux/arm64），由 GitHub Actions 原生 runner 分别构建后合并。

---

## 特性

| 能力 | 说明 |
|---|---|
| **单容器** | 内核、面板、订阅管理跑在同一容器，supervisord 管理三进程 |
| **订阅管理页** | 网页上添加/删除订阅、改地区白名单与延迟阈值、一键刷新 |
| **双订阅合并** | 支持任意多个订阅，第 2 个起自动加 `S2-`/`S3-` 前缀防重名 |
| **延迟过滤** | 拉取 → 白名单过滤 → 逐个测速 → 剔除超时/高延迟节点 |
| **负载均衡** | 支持 `sticky-sessions` / `round-robin` / `consistent-hashing` |
| **多架构** | amd64 + arm64 双架构镜像 |

---

## 快速开始

### 方式 1：用构建好的镜像

```bash
# 1. 下载 compose 与 env 模板
curl -O https://raw.githubusercontent.com/countossbot/mihomo-allinone/main/docker-compose.allinone.yml
curl -O https://raw.githubusercontent.com/countossbot/mihomo-allinone/main/.env.example

# 2. 生成密钥并填入 .env（该文件不会进版本库）
cp .env.example .env
sed -i '' "s/^MIHOMO_SECRET=.*/MIHOMO_SECRET=$(openssl rand -hex 16)/" .env

# 3. 启动
docker compose -f docker-compose.allinone.yml up -d
```

### 方式 2：本地构建

```bash
git clone https://github.com/countossbot/mihomo-allinone.git
cd mihomo-allinone
docker compose -f docker-compose.allinone.yml up -d --build
```

---

## 端口

| 端口 | 用途 |
|---|---|
| `8080` | metacubexd 面板 |
| `9090` | 内核控制 API（面板后端填这个） |
| `7890` | 混合代理口（HTTP + SOCKS5） |
| `8765` | **订阅管理页** |

---

## 使用

### 1. 打开订阅管理页

```
http://127.0.0.1:8765
```

在这里：
- **添加/删除订阅**（支持多个，自动合并）
- **设置地区白名单**（如 `DE NL GB US JP SG`，留空=全部）
- **设置延迟阈值**（超过则剔除，默认 3000ms）
- **选择负载均衡策略**
- 点「**保存并刷新**」→ 自动完成拉取、筛选、测速、重启内核

设置会保存到 `/data/settings.json`，重启容器不丢失。

### 2. 打开面板

```
http://127.0.0.1:8080
```

后端地址已预填 `http://127.0.0.1:9090`，填入你在 `.env` 里设置的密钥即可。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MIHOMO_SECRET` | **必填**（无默认值） | 内核控制密钥，用 `openssl rand -hex 16` 生成 |
| `CONTROL_PORT` | `8080` | 面板端口 |
| `REFRESH_PORT` | `8765` | 订阅管理页端口 |
| `REGION_WHITELIST` | `DE NL GB US JP SG FI DK` | 地区白名单（首次启动的默认值） |
| `MAX_DELAY` | `3000` | 延迟阈值 ms |
| `TEST_TIMEOUT` | `3000` | 测速超时 ms |
| `SUB_URLS` | 空 | 订阅地址，逗号分隔（**建议留空，在管理页里添加**） |

> 订阅、白名单、阈值通过**管理页**修改更直观，环境变量只是首次启动的默认值。

---

## 数据持久化

挂载 `/data` 后，以下内容会保留：

```
/data/config.yaml           内核运行配置（脚本生成）
/data/settings.json         管理页设置（订阅/白名单/阈值）
/data/nodes-filtered.json   上次筛选明细
/data/geoip.dat             地理数据（首次启动自动下载）
/data/geoip.metadb
/data/geosite.dat
```

---

## 架构

```
┌─ 容器 mihomo-allinone ─────────────────────┐
│  supervisord                               │
│   ├─ mihomo   内核     :9090 / :7890       │
│   ├─ panel    面板     :8080（Nuxt SSR）    │
│   └─ refresh  管理页   :8765（Python）      │
│         └─ 调 supervisorctl 重启 mihomo     │
└────────────────────────────────────────────┘
```

> 刷新服务与内核同容器，重启内核只需 `supervisorctl restart mihomo`，
> **不需要挂载 docker.sock**。

---

## 刷新流程

```
点击「保存并刷新」
   ↓
1. 用内核拉取所有订阅（自动转换 base64/Clash 格式）
   ↓
2. 按地区白名单过滤节点名（第 2 个订阅起加 S2- 前缀）
   ↓
3. 起临时内核，逐个测速
   ↓
4. 剔除超时与超过阈值的节点
   ↓
5. 生成 config.yaml（含各订阅独立测速组 + 跨订阅合并组）
   ↓
6. supervisorctl restart mihomo
```

---

## 镜像构建

`.github/workflows/build.yml`：

- **build job**：矩阵 `linux/amd64 → ubuntu-24.04`、`linux/arm64 → ubuntu-24.04-arm`
  - 用**原生 runner**，避免 QEMU 模拟（快 10 倍以上）
  - 各自 buildx 按 digest 推送（不打 tag）
- **merge job**：收集两个 digest，用 `docker buildx imagetools create` 合成多架构 manifest

产出 tag：`latest`、`sha-<7位>`、打 tag 时附 `v1.0.0` 与 `1.0.0`。

---

## 致谢

- [mihomo](https://github.com/MetaCubeX/mihomo) —— 内核
- [metacubexd](https://github.com/MetaCubeX/metacubexd) —— 面板
