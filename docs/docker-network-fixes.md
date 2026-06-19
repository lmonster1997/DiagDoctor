# Docker 构建 — 国内网络问题与解决方案

> 记录日期：2026-06-19  
> 环境：Windows 11 + Docker Desktop + WSL2

---

## 最终状态 ✅

```powershell
docker compose up -d
```

```
[+] up 6/6
 ✔ Network diagdoctor-main_default     Created
 ✔ Container diagdoctor-redis          Healthy
 ✔ Container diagdoctor-otel-collector Started
 ✔ Container diagdoctor-postgres       Healthy
 ✔ Container diagdoctor-demo-backend   Started
 ✔ Container diagdoctor-demo-frontend  Started
```

---

## 遇到的问题与解决

### 问题 1：Docker Hub 镜像拉取失败

**现象**：
```
Error response from daemon: failed to resolve reference "docker.io/otel/..."
dial tcp 128.121.146.109:443: connectex: A connection attempt failed
```

**原因**：国内直连 Docker Hub（`registry-1.docker.io`）被 DNS 污染 / 443 端口不通。

**解决**：配置 Docker 镜像加速器

```json
// Docker Desktop → Settings → Docker Engine
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me"
  ]
}
```

备用镜像（按可用性优先级）：
| 镜像 | 地址 |
|------|------|
| 1ms | `https://docker.1ms.run` |
| 轩园 | `https://docker.xuanyuan.me` |
| Rat | `https://hub.rat.dev` |

---

### 问题 2：ghcr.io 拉取 uv 镜像超时

**现象**：
```
#15 sha256:... 15.73MB / 25.73MB 2258.0s   ← 拖了 37 分钟还没完
COPY --from=ghcr.io/astral-sh/uv:latest
```

**原因**：`ghcr.io`（GitHub Container Registry）同样被限速。

**解决**：不在 Dockerfile 中用 `COPY --from=ghcr.io`，改为 `pip install uv`：

```dockerfile
# ❌ 原写法（依赖 ghcr.io）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ✅ 修复后（走 PyPI）
RUN pip install uv --no-cache-dir
```

---

### 问题 3：pnpm 版本与 Node.js 不兼容

**现象**：
```
warn: This version of pnpm requires at least Node.js v22.13
warn: The current version of Node.js is v20.20.2
Error [ERR_UNKNOWN_BUILTIN_MODULE]: No such built-in module: node:sqlite
```

**原因**：`corepack prepare pnpm@latest` 拉到了 v11.x，需要 Node 22+；`node:20-alpine` 只有 Node 20。

**解决**：锁死 pnpm 主版本号：

```dockerfile
# ❌ 原写法
RUN corepack enable && corepack prepare pnpm@latest --activate

# ✅ 修复后
RUN corepack enable && corepack prepare pnpm@9 --activate
```

> 规则：`node:20` → `pnpm@9`；`node:22` → `pnpm@latest`（v11）

---

### 问题 4：apt-get / pip / npm 下载极慢

**现象**：`apt-get update` 卡在 `deb.debian.org`、`uv sync` 下载大包（grpcio 6.5MB）超时。

**原因**：Docker 容器走 WSL2 独立网络栈，**不会自动走宿主机 VPN**。

**解决方案（二选一）**：

#### 方案 A：Docker 代理（有 VPN 时推荐） ✅

1. 打开 Clash / V2Ray → 确认 HTTP 代理端口（Clash 通常是 `7890`）

2. Docker Desktop → ⚙️ Settings → Resources → Proxies：
   ```
   HTTP Proxy:  127.0.0.1:7890
   HTTPS Proxy: 127.0.0.1:7890
   ```
   点 Apply & Restart。

3. **同时打开 Clash 的"系统代理"**（关键！Docker 需要系统代理而非仅浏览器代理）。

#### 方案 B：国内镜像（无 VPN 时） ⚠️

在 Dockerfile 中配置各包管理器的国内镜像：

```dockerfile
# Debian apt
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources

# PyPI（pip + uv）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV UV_INDEX_URL=${PIP_INDEX_URL}

# npm / pnpm
RUN npm config set registry https://registry.npmmirror.com
```

> 镜像方案缺点：大二进制包（grpcio、cryptography）可能仍然不稳定，需要额外配置超时和并发限制。

---

### 问题 5：uv sync HTTP 超时参数错误

**现象**：
```
error: unexpected argument '--http-timeout' found
```

**原因**：`uv` 不支持 `--http-timeout` 命令行参数。

**解决**：改用环境变量：

```dockerfile
# ❌ 错误写法
RUN uv sync --http-timeout 300

# ✅ 正确写法
ENV UV_HTTP_TIMEOUT=300
RUN uv sync
```

---

## 最终 Dockerfile（VPN 代理版）

### `demo-app/backend/Dockerfile`

```dockerfile
FROM python:3.11-slim AS builder
WORKDIR /app

# Debian mirror — Docker bypasses host VPN
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*

RUN pip install uv --no-cache-dir
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./
ENV UV_HTTP_TIMEOUT=300
RUN uv sync --frozen --no-dev --no-install-project

# Runtime stage
FROM python:3.11-slim AS runtime
WORKDIR /app
RUN groupadd -r appuser && useradd -r -g appuser appuser
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/
ENV PATH="/app/.venv/bin:$PATH"
ENV OTEL_SERVICE_NAME=demo-backend
EXPOSE 8000
USER appuser
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### `demo-app/frontend/Dockerfile`

```dockerfile
FROM node:20-alpine AS builder
WORKDIR /app

RUN corepack enable && corepack prepare pnpm@9 --activate
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

FROM nginx:alpine AS runtime
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:80/ || exit 1
CMD ["nginx", "-g", "daemon off;"]
```

---

## 新机器验证清单

```powershell
# 1. 确保 Docker Desktop 运行
docker --version

# 2. 配置 Docker 镜像加速（Settings → Docker Engine）
#    "registry-mirrors": ["https://docker.1ms.run", "https://docker.xuanyuan.me"]

# 3. 配置 Docker 代理（Settings → Resources → Proxies）
#    HTTP 127.0.0.1:7890  |  HTTPS 127.0.0.1:7890

# 4. 启动
git clone <repo>
cd DiagDoctor
docker compose up -d

# 5. 验证
docker compose ps
# 浏览器打开 http://localhost:3000
```

---

## 故障排查速查表

| 症状 | 可能原因 | 检查项 |
|------|---------|--------|
| `docker compose up` 直接报错 | Docker Desktop 没启动 | 托盘区 🐳 图标 |
| `dial tcp ... connectex` | Docker Hub 被墙 | registry-mirrors 配置 |
| `apt-get update` 卡住 | deb.debian.org 不通 | Debian mirror / Docker 代理 |
| `uv sync` 下载卡住 | PyPI 不通 | PyPI mirror / Docker 代理 |
| `pnpm install` 失败 | npm 不通 + pnpm 版本 | pnpm@9 + npm mirror / 代理 |
| `node:sqlite` 报错 | pnpm v11 需要 Node 22 | 锁死 pnpm@9 |
| `ghcr.io` 超时 | ghcr 被墙 | pip install uv 替代 |
