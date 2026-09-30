# ============================================================
# mihomo-allinone —— 单容器整合：内核 + 面板 + 刷新服务
#
# 多阶段构建：
#   1. panel-src  : 从官方 metacubexd 镜像抽出 Nuxt 产物
#   2. runtime    : node + mihomo 内核 + 刷新服务，supervisord 管理
#
# 对外端口：
#   8080  面板（+ 控制 API 反代）
#   7890  混合代理口（HTTP + SOCKS5）
#   8765  刷新按钮页
# ============================================================

# ---------- 阶段 1：抽取面板产物 ----------
FROM ghcr.io/metacubex/metacubexd:latest AS panel-src
# 本镜像内含 /app/.output/{public,server}，直接复制即可

# ---------- 阶段 2：运行时 ----------
FROM node:22-alpine

LABEL org.opencontainers.image.title="mihomo-allinone" \
      org.opencontainers.image.description="mihomo kernel + metacubexd panel + node refresh service" \
      org.opencontainers.image.source="https://github.com/YOURNAME/mihomo-allinone"

# ---- 系统依赖 ----
#   supervisor : 多进程管理
#   bash/curl  : 脚本依赖
#   tzdata     : 时区
#   py3        : 刷新服务与筛选脚本
RUN apk add --no-cache \
      supervisor bash curl tzdata ca-certificates tini \
      python3 py3-pip \
 && pip install --no-cache-dir --break-system-packages pyyaml \
 && rm -rf /var/cache/apk/*

# ---- mihomo 内核（从官方镜像取二进制）----
COPY --from=metacubex/mihomo:latest /mihomo /usr/local/bin/mihomo

# ---- 面板产物 ----
COPY --from=panel-src /app/.output /app/panel

# ---- 应用文件（先复制脚本，再赋权限）----
COPY config.js /app/panel/public/config.js
COPY scripts/ /app/scripts/
COPY supervisord.conf /etc/supervisord.conf

RUN mkdir -p /data /var/log/supervisor /var/run && \
    chmod +x /app/scripts/*.sh 2>/dev/null || true

# ---- 修正 node 路径（node:alpine 用 /usr/local/bin/node）----
RUN ln -sf /usr/local/bin/node /usr/bin/node 2>/dev/null || true

# ---- 环境变量（可被 compose 覆盖）----
ENV \
    TZ=Asia/Shanghai \
    CONTROL_PORT=8080 \
    CLASH_API_PORT=9090 \
    MIXED_PORT=7890 \
    REFRESH_PORT=8765 \
    MIHOMO_SECRET=change-me-in-compose \
    REGION_WHITELIST="DE NL GB US JP SG FI DK" \
    MAX_DELAY=3000 \
    TEST_TIMEOUT=3000

VOLUME ["/data"]

EXPOSE 8080 7890 8765

# tini 处理信号，supervisord 管理三个进程
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
