# ============================================================
# Stage 1: Build the React / Vite frontend
# ============================================================
FROM node:20-alpine AS builder

WORKDIR /app

# Configure npm mirror & install pnpm
RUN npm config set registry https://registry.npmmirror.com && \
    npm install -g pnpm@9

# ---- Dependency layer (cache-friendly: copy dep files first) ----
COPY package.json pnpm-lock.yaml .npmrc ./

# Ensure devDependencies are installed (pnpm skips them if NODE_ENV=production)
ENV NODE_ENV=development

# Configure pnpm mirror & install (hoist enabled via .npmrc)
RUN pnpm config set registry https://registry.npmmirror.com && \
    pnpm install --frozen-lockfile

# Copy application source
COPY . .

# Build for production
RUN pnpm build

# ============================================================
# Stage 2: Serve with nginx
# ============================================================
FROM nginx:alpine AS runtime

# Install wget for health check
RUN apk add --no-cache wget

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
