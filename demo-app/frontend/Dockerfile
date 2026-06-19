# ============================================================
# Stage 1: Build the React / Vite frontend
# ============================================================
FROM node:20-alpine AS builder

WORKDIR /app

# Install pnpm v9 (compatible with Node 20; v11+ requires Node 22+)
RUN corepack enable && corepack prepare pnpm@9 --activate

# ---- Dependency layer (cache-friendly: copy dep files first) ----
COPY package.json pnpm-lock.yaml ./

# Install dependencies (use VPN for npm registry access)
RUN pnpm install --frozen-lockfile

# Copy application source
COPY . .

# Build for production
RUN pnpm build

# ============================================================
# Stage 2: Serve with nginx
# ============================================================
FROM nginx:alpine AS runtime

# Remove default nginx config
RUN rm /etc/nginx/conf.d/default.conf

# Copy our custom nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy built assets from the builder stage
COPY --from=builder /app/dist /usr/share/nginx/html

# Expose port
EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:80/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
