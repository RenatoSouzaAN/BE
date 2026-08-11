from datetime import datetime
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import psycopg

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

app = FastAPI(
    title="Task API",
    description="PostgreSQL to-do list CRUD API (AI rematch A3 version).",
    version="3.0",
)


class Task(BaseModel):
    id: int
    title: str
    done: bool


class TaskCreate(BaseModel):
    title: str | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    done: bool | None = None


SEED_TASKS: list[dict] = [
    {"id": 1, "title": "Task 1", "done": True},
    {"id": 2, "title": "Task 2", "done": False},
    {"id": 3, "title": "Task 3", "done": True},
]


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    """Keep errors flat as {\"error\": \"...\"} instead of FastAPI's detail wrapper."""
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


def get_connection() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def row_to_task(row: tuple) -> dict:
    return {
        "id": row[0],
        "title": row[1],
        "done": bool(row[2]),
    }


def init_db() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    done BOOLEAN NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cur.execute("SELECT COUNT(*) FROM tasks")
            if cur.fetchone()[0] == 0:
                now = datetime.now().isoformat()
                for task in SEED_TASKS:
                    cur.execute(
                        """
                        INSERT INTO tasks (id, title, done, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (task["id"], task["title"], task["done"], now, now),
                    )
                cur.execute(
                    "SELECT setval(pg_get_serial_sequence('tasks', 'id'), "
                    "(SELECT COALESCE(MAX(id), 1) FROM tasks))"
                )
            conn.commit()


init_db()


@app.get("/", summary="API info")
async def root():
    """Return API name, version, storage, and available endpoints."""
    return {
        "name": "Task API",
        "version": "3.0",
        "storage": "postgresql + psycopg",
        "endpoints": [
            "/health",
            "/reset",
            "/tasks",
            "/tasks/stats",
            "/tasks/{id}",
        ],
    }


@app.get("/health", summary="Health check")
async def health():
    """Return a simple status payload used to verify the server is alive."""
    return {"status": "ok"}


@app.post("/reset", summary="Reset tasks")
async def reset_tasks():
    """Delete all tasks and restore the original three seed tasks."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE tasks RESTART IDENTITY")
            for task in SEED_TASKS:
                cur.execute(
                    """
                    INSERT INTO tasks (title, done, created_at, updated_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (task["title"], task["done"], now, now),
                )
            conn.commit()
    return {"message": "Tasks list reset successfully."}


@app.get("/tasks", summary="List tasks")
async def list_tasks(done: bool | None = None, search: str | None = None):
    """List all tasks, optionally filtered by done status and/or title search."""
    clauses: list[str] = []
    params: list = []

    if done is not None:
        clauses.append("done = %s")
        params.append(done)

    if search is not None:
        clauses.append("LOWER(title) LIKE LOWER(%s)")
        params.append(f"%{search}%")

    query = "SELECT id, title, done FROM tasks"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    return [row_to_task(row) for row in rows]


@app.get("/tasks/stats", summary="Task stats")
async def task_stats():
    """Return total, done, and pending task counts from the database."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*),
                    COUNT(*) FILTER (WHERE done),
                    COUNT(*) FILTER (WHERE NOT done)
                FROM tasks
                """
            )
            total, done_count, pending = cur.fetchone()
    return {
        "total": total or 0,
        "done": done_count or 0,
        "pending": pending or 0,
    }


@app.get("/tasks/{task_id}", summary="Get task")
async def get_task(task_id: int):
    """Return a single task by ID, or 404 if it does not exist."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, done FROM tasks WHERE id = %s",
                (task_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "Task not found"})
    return row_to_task(row)


@app.post("/tasks", status_code=201, summary="Create task")
async def create_task(payload: TaskCreate):
    """Create a new task with done=false and current timestamps."""
    if not payload.title or not payload.title.strip():
        raise HTTPException(status_code=400, detail={"error": "Title is required."})

    now = datetime.now().isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (title, done, created_at, updated_at)
                VALUES (%s, %s, %s, %s)
                RETURNING id, title, done
                """,
                (payload.title.strip(), False, now, now),
            )
            row = cur.fetchone()
            conn.commit()
    return row_to_task(row)


@app.put("/tasks/{task_id}", summary="Update task")
async def update_task(task_id: int, payload: TaskUpdate):
    """Update title and/or done for an existing task; touches updated_at."""
    if payload.title is None and payload.done is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "At least one field to update is required."},
        )
    if payload.title is not None and not payload.title.strip():
        raise HTTPException(status_code=400, detail={"error": "Title cannot be empty."})

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, done FROM tasks WHERE id = %s",
                (task_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail={"error": "Task not found"})

            title = payload.title.strip() if payload.title is not None else row[1]
            done = payload.done if payload.done is not None else row[2]
            now = datetime.now().isoformat()

            cur.execute(
                """
                UPDATE tasks
                SET title = %s, done = %s, updated_at = %s
                WHERE id = %s
                RETURNING id, title, done
                """,
                (title, done, now, task_id),
            )
            updated = cur.fetchone()
            conn.commit()
    return row_to_task(updated)


@app.delete("/tasks/{task_id}", status_code=204, summary="Delete task")
async def delete_task(task_id: int):
    """Delete a task by ID. Returns 204 with an empty body on success."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            deleted = cur.rowcount
            conn.commit()
    if deleted == 0:
        raise HTTPException(status_code=404, detail={"error": "Task not found"})
    return Response(status_code=204)
