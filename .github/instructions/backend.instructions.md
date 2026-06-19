# Backend Development Instructions

> **applyTo**: `demo-app/backend/**`, `doctor/**`, `bug-factory/**`
> **description**: "Use when: working on FastAPI backend, SQLAlchemy models, Alembic migrations, Pydantic schemas, JWT auth, OpenTelemetry, or LangGraph agents. Covers Python async patterns, structured logging, and API design conventions."

---

## FastAPI 约定

### 路由组织
- 路由文件放在 `app/api/`，按资源命名：`projects.py`, `tasks.py`, `comments.py`
- 每个路由模块创建 `APIRouter(prefix="/api/xxx", tags=["xxx"])`
- 在 `main.py` 中统一 `app.include_router(xxx.router)`

### 依赖注入
- 数据库 session 通过 `Depends(get_db)` 注入
- 当前用户通过 `Depends(get_current_user)` 注入（JWT 解析）
- 不要手动创建 session 或读取 token

```python
from app.database import get_db
from app.auth.deps import get_current_user

@router.get("/{id}")
async def get_xxx(
    id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    ...
```

### 错误处理
- 使用 `HTTPException` + status codes，不用自定义异常类
- 用 `structlog` 记录结构化日志，不用 `print()` 或 `logging` 直接调用

```python
import structlog
logger = structlog.get_logger()

logger.info("task_created", task_id=str(task.id), user_id=str(current_user.id))
```

---

## SQLAlchemy 模型约定

### 基类
- 所有模型继承 `app.database.Base`（`DeclarativeBase` + `AsyncAttrs`）
- 表名用复数 `snake_case`：`__tablename__ = "projects"`

### 字段定义
- 主键用 `UUID(as_uuid=True)`，默认 `uuid.uuid4`
- 时间字段用 `DateTime(timezone=True)`
- 关系用 `Mapped[list["Other"]]` + `relationship("Other", back_populates="...")`

```python
from uuid import UUID, uuid4
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

class Task(Base):
    __tablename__ = "tasks"
    
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("projects.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    
    project: Mapped["Project"] = relationship("Project", back_populates="tasks")
```

### 迁移
- **不要修改已有迁移文件**（`alembic/versions/` 下的现有文件）
- 新增迁移：`uv run alembic revision -m "description"` 然后编辑生成的 py 文件
- 执行迁移：`uv run alembic upgrade head`

---

## Pydantic Schema 约定

- Request schema 用 `class XxxCreate(BaseModel)`、`class XxxUpdate(BaseModel)`
- Response schema 用 `class XxxResponse(BaseModel)`，配置 `model_config = {"from_attributes": True}`
- 全部字段加 `Field(description="...")` 文档说明

```python
from pydantic import BaseModel, Field
from uuid import UUID

class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="Task title")
    project_id: UUID = Field(..., description="Project this task belongs to")
    priority: int = Field(default=0, ge=0, le=5, description="Priority 0-5")

class TaskResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: UUID
    title: str
    ...
```

---

## 认证（JWT）

- 登录/注册路由在 `app/api/auth.py`
- JWT 工具函数在 `app/auth/utils.py`：`create_access_token()`, `verify_token()`
- 依赖注入在 `app/auth/deps.py`：`get_current_user()`, `get_current_active_user()`
- Token 通过 `Authorization: Bearer <token>` 传递

---

## OpenTelemetry

- **必须在 FastAPI app 实例化之前**调用 `init_observability()`
- 初始化代码在 `app/observability.py`
- 环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT` 控制上报地址

---

## 测试约定

- 测试文件在 `tests/` 下，命名 `test_xxx.py`
- 使用 `pytest` + `pytest-asyncio`，函数加 `@pytest.mark.asyncio`
- 数据库测试用 `pytest-asyncio` + 事务回滚隔离

```python
import pytest

@pytest.mark.asyncio
async def test_create_task(client, db_session):
    response = await client.post("/api/tasks", json={...})
    assert response.status_code == 201
```

---

## 禁止事项

- ❌ 不要用同步 SQLAlchemy（全部 async）
- ❌ 不要在路由中直接访问 request body（用 Pydantic schema 校验）
- ❌ 不要硬编码配置值（用 `app.config.settings`）
- ❌ 不要绕过权限检查
- ❌ 不要裸写 SQL（用 SQLAlchemy ORM）
