"""
Seed script — populate the TaskFlow database with demo data.

Creates (idempotently):
  - admin user  : admin@example.com  / Admin123!
  - alice user  : alice@example.com  / Alice123!
  - Demo Project (owned by admin)
  - 30 tasks spread across todo / doing / done statuses
  - Comments on a subset of tasks

Usage:
  cd demo-app/backend
  uv run python scripts/seed.py

Or via docker compose:
  docker compose exec demo-backend python scripts/seed.py
"""

import asyncio
import sys
from pathlib import Path

# Ensure the 'app' package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, engine
from app.models import Comment, Project, Task, TaskStatus, User  # noqa: E402
from app.auth.utils import hash_password  # noqa: E402

# ── Seed Data ──────────────────────────────────────────────────────

SEED_USERS = [
    {
        "email": "admin@example.com",
        "password": "Admin123!",
        "display_name": "Admin",
    },
    {
        "email": "alice@example.com",
        "password": "Alice123!",
        "display_name": "Alice",
    },
]

SEED_PROJECT = {
    "name": "Demo Project",
    "description": "A sample project for the TaskFlow demo application.",
}

# 30 tasks with varied statuses & priorities
SEED_TASKS = [
    # ── TODO (10 tasks) ──
    {"title": "Set up CI/CD pipeline", "status": "todo", "priority": 3, "assignee_idx": 0},
    {"title": "Write API documentation", "status": "todo", "priority": 2, "assignee_idx": 1},
    {"title": "Design database schema for audit log", "status": "todo", "priority": 3, "assignee_idx": None},
    {"title": "Research WebSocket libraries", "status": "todo", "priority": 1, "assignee_idx": 0},
    {"title": "Add rate limiting to auth endpoints", "status": "todo", "priority": 3, "assignee_idx": None},
    {"title": "Create onboarding email template", "status": "todo", "priority": 2, "assignee_idx": 1},
    {"title": "Set up staging environment", "status": "todo", "priority": 3, "assignee_idx": 0},
    {"title": "Write unit tests for comment service", "status": "todo", "priority": 2, "assignee_idx": 1},
    {"title": "Investigate memory leak in worker process", "status": "todo", "priority": 3, "assignee_idx": None},
    {"title": "Plan v2.0 feature roadmap", "status": "todo", "priority": 1, "assignee_idx": 0},

    # ── DOING (10 tasks) ──
    {"title": "Implement task search with full-text index", "status": "doing", "priority": 3, "assignee_idx": 0},
    {"title": "Refactor project service to use repository pattern", "status": "doing", "priority": 2, "assignee_idx": 1},
    {"title": "Fix pagination bug on task list page", "status": "doing", "priority": 3, "assignee_idx": 0},
    {"title": "Add drag-and-drop sorting to task board", "status": "doing", "priority": 2, "assignee_idx": 1},
    {"title": "Optimize N+1 query in task list endpoint", "status": "doing", "priority": 3, "assignee_idx": None},
    {"title": "Migrate from moment.js to date-fns", "status": "doing", "priority": 1, "assignee_idx": 0},
    {"title": "Add Sentry error tracking to frontend", "status": "doing", "priority": 2, "assignee_idx": 1},
    {"title": "Write integration tests for auth flow", "status": "doing", "priority": 2, "assignee_idx": None},
    {"title": "Update dependencies to latest versions", "status": "doing", "priority": 1, "assignee_idx": 0},
    {"title": "Design new comment notification system", "status": "doing", "priority": 2, "assignee_idx": 1},

    # ── DONE (10 tasks) ──
    {"title": "Initialize project repository", "status": "done", "priority": 3, "assignee_idx": 0},
    {"title": "Set up PostgreSQL and Redis containers", "status": "done", "priority": 3, "assignee_idx": 0},
    {"title": "Create user registration and login API", "status": "done", "priority": 3, "assignee_idx": 1},
    {"title": "Implement JWT authentication middleware", "status": "done", "priority": 3, "assignee_idx": 1},
    {"title": "Build project CRUD endpoints", "status": "done", "priority": 2, "assignee_idx": 0},
    {"title": "Build task CRUD endpoints", "status": "done", "priority": 3, "assignee_idx": 1},
    {"title": "Add comment functionality to tasks", "status": "done", "priority": 2, "assignee_idx": 0},
    {"title": "Create React frontend scaffold with Vite", "status": "done", "priority": 3, "assignee_idx": 1},
    {"title": "Implement task board page with three columns", "status": "done", "priority": 3, "assignee_idx": 0},
    {"title": "Add Tailwind CSS and shadcn/ui components", "status": "done", "priority": 2, "assignee_idx": 1},
]

# Seed comments for some tasks (by task title)
SEED_COMMENTS_BY_TASK: dict[str, list[dict]] = {
    "Fix pagination bug on task list page": [
        {"author_idx": 0, "content": "Looks like the offset is calculated incorrectly when page > 1."},
        {"author_idx": 1, "content": "I can reproduce this — page 3 shows duplicates from page 2."},
    ],
    "Optimize N+1 query in task list endpoint": [
        {"author_idx": 0, "content": "We should use selectinload() or joinedload() to prefetch comments."},
        {"author_idx": 1, "content": "Agreed. The current code makes a separate query per task — that's the bottleneck."},
    ],
    "Add drag-and-drop sorting to task board": [
        {"author_idx": 0, "content": "@dnd-kit looks like the best option. I've drafted a prototype."},
    ],
    "Build task CRUD endpoints": [
        {"author_idx": 1, "content": "All CRUD endpoints are passing manual tests. Ready for review."},
    ],
    "Create user registration and login API": [
        {"author_idx": 0, "content": "JWT tokens working. Let's add refresh token support in the next sprint."},
    ],
}


# ── Helpers ────────────────────────────────────────────────────────

async def _user_exists(session: AsyncSession, email: str) -> User | None:
    """Check if a user with the given email already exists."""
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def _project_exists(session: AsyncSession, name: str) -> Project | None:
    """Check if a project with the given name already exists."""
    result = await session.execute(select(Project).where(Project.name == name))
    return result.scalar_one_or_none()


async def _task_exists(session: AsyncSession, title: str, project_id) -> Task | None:
    """Check if a task with the given title already exists in the project."""
    result = await session.execute(
        select(Task).where(Task.title == title, Task.project_id == project_id)
    )
    return result.scalar_one_or_none()


async def _comment_exists(session: AsyncSession, task_id, author_id, content: str) -> bool:
    """Check if a specific comment already exists."""
    result = await session.execute(
        select(Comment).where(
            Comment.task_id == task_id,
            Comment.author_id == author_id,
            Comment.content == content,
        )
    )
    return result.scalar_one_or_none() is not None


# ── Main ───────────────────────────────────────────────────────────

async def seed() -> None:
    """Run the seed process — idempotent."""
    print("🌱 Starting seed...")

    async with async_session_factory() as session:
        # ------------------------------------------------------------
        # 1. Create users
        # ------------------------------------------------------------
        users: dict[int, User] = {}
        for idx, u in enumerate(SEED_USERS):
            existing = await _user_exists(session, u["email"])
            if existing:
                users[idx] = existing
                print(f"  ⏭  User already exists: {u['email']}")
            else:
                new_user = User(
                    email=u["email"],
                    hashed_password=hash_password(u["password"]),
                    display_name=u["display_name"],
                )
                session.add(new_user)
                await session.flush()
                users[idx] = new_user
                print(f"  ✅ Created user: {u['email']}")

        # ------------------------------------------------------------
        # 2. Create project
        # ------------------------------------------------------------
        existing_project = await _project_exists(session, SEED_PROJECT["name"])
        if existing_project:
            project = existing_project
            print(f"  ⏭  Project already exists: {SEED_PROJECT['name']}")
        else:
            project = Project(
                name=SEED_PROJECT["name"],
                description=SEED_PROJECT["description"],
                owner_id=users[0].id,  # owned by admin
            )
            session.add(project)
            await session.flush()
            print(f"  ✅ Created project: {SEED_PROJECT['name']}")

        # ------------------------------------------------------------
        # 3. Create tasks
        # ------------------------------------------------------------
        created_tasks: dict[str, Task] = {}
        created_count = 0
        skipped_count = 0

        for task_data in SEED_TASKS:
            existing = await _task_exists(session, task_data["title"], project.id)
            if existing:
                created_tasks[task_data["title"]] = existing
                skipped_count += 1
                continue

            assignee_id = None
            if task_data["assignee_idx"] is not None:
                assignee_id = users[task_data["assignee_idx"]].id

            new_task = Task(
                project_id=project.id,
                title=task_data["title"],
                description=f"Task: {task_data['title']}",
                status=TaskStatus(task_data["status"]),
                priority=task_data["priority"],
                assignee_id=assignee_id,
            )
            session.add(new_task)
            await session.flush()
            created_tasks[task_data["title"]] = new_task
            created_count += 1

        print(f"  ✅ Created {created_count} tasks ({skipped_count} already existed)")

        # ------------------------------------------------------------
        # 4. Create comments
        # ------------------------------------------------------------
        comment_count = 0
        for task_title, comment_list in SEED_COMMENTS_BY_TASK.items():
            task = created_tasks.get(task_title)
            if task is None:
                continue
            for c in comment_list:
                author = users[c["author_idx"]]
                if await _comment_exists(session, task.id, author.id, c["content"]):
                    continue
                comment = Comment(
                    task_id=task.id,
                    author_id=author.id,
                    content=c["content"],
                )
                session.add(comment)
                comment_count += 1

        print(f"  ✅ Created {comment_count} comments")

        # ------------------------------------------------------------
        # Commit
        # ------------------------------------------------------------
        await session.commit()

    print("🎉 Seed complete!")


async def main() -> None:
    """Entry point: verify DB connection then seed."""
    # Quick connectivity check
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        print("✅ Database connection OK")
    except Exception as exc:
        print(f"❌ Cannot connect to database: {exc}")
        print("   Make sure PostgreSQL is running and DATABASE_URL is correct.")
        sys.exit(1)

    await seed()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
