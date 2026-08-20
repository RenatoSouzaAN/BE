from contextlib import asynccontextmanager
from datetime import datetime
import os
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
import psycopg
from supabase import Client, create_client
from supabase_auth.errors import AuthApiError

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
security = HTTPBearer(auto_error=False)


class UserCredentials(BaseModel):
    email: str | None = None
    password: str | None = None


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


def iso_datetime(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Task API",
    description="PostgreSQL to-do list CRUD API with Supabase auth (AI rematch A4 version).",
    version="4.0",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    """Keep errors flat as {"error": "..."} instead of FastAPI's detail wrapper."""
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    """Turn Pydantic/FastAPI validation errors into the same flat error shape."""
    first = exc.errors()[0] if exc.errors() else {}
    return JSONResponse(
        status_code=400,
        content={"error": first.get("msg", "Invalid request")},
    )


def error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": message})


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
):
    """Resolve the logged-in Supabase user from a Bearer access token."""
    if not credentials or not credentials.credentials:
        raise error(401, "Access token required")

    try:
        response = supabase.auth.get_user(credentials.credentials)
    except AuthApiError:
        raise error(401, "Invalid or expired token")

    if response.user is None:
        raise error(401, "Invalid or expired token")

    return response.user


@app.get("/", summary="API info")
async def root():
    """Return API name, version, storage, auth, and available endpoints."""
    return {
        "name": "Task API",
        "version": "4.0",
        "storage": "postgresql + psycopg",
        "auth": "supabase",
        "endpoints": [
            "/health",
            "/auth/signup",
            "/auth/login",
            "/auth/logout",
            "/protected/profile",
            "/profile/dashboard",
            "/public/info",
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


@app.post("/auth/signup", status_code=201, summary="Sign up")
async def signup(user: UserCredentials):
    """Create a new user in Supabase from email and password."""
    if not user.email or not user.password:
        raise error(400, "Email and password are required")

    try:
        response = supabase.auth.sign_up(
            {"email": user.email, "password": user.password}
        )
    except AuthApiError as exc:
        raise error(400, str(exc))

    if response.user is None:
        raise error(400, "Signup failed")

    return {
        "id": response.user.id,
        "email": response.user.email,
        "created_at": iso_datetime(response.user.created_at),
    }


@app.post("/auth/login", summary="Log in")
async def login(user: UserCredentials):
    """Authenticate with Supabase and return access and refresh tokens."""
    if not user.email or not user.password:
        raise error(400, "Email and password are required")

    try:
        response = supabase.auth.sign_in_with_password(
            {"email": user.email, "password": user.password}
        )
    except AuthApiError:
        raise error(401, "Invalid login credentials")

    if response.session is None:
        raise error(401, "Invalid login credentials")

    return {
        "access_token": response.session.access_token,
        "refresh_token": response.session.refresh_token,
    }


@app.post("/auth/logout", status_code=204, summary="Log out")
async def logout(_user=Depends(get_current_user)):
    """Invalidate the current Supabase session. Returns 204 with an empty body."""
    try:
        supabase.auth.sign_out()
    except AuthApiError:
        raise error(401, "Invalid or expired token")
    return Response(status_code=204)


@app.get("/protected/profile", summary="Protected profile")
async def get_protected_profile(user=Depends(get_current_user)):
    """Return the logged-in user's id, email, and creation date."""
    return {
        "id": user.id,
        "email": user.email,
        "created_at": iso_datetime(user.created_at),
    }


@app.get("/profile/dashboard", summary="Protected dashboard")
async def get_profile_dashboard(user=Depends(get_current_user)):
    """Return the logged-in user's id and email for the dashboard."""
    return {
        "id": user.id,
        "email": user.email,
    }


@app.get("/public/info", summary="Public info")
async def get_public_info():
    """Return a welcome message that does not require authentication."""
    return {"message": "Welcome, Stranger! This info is public."}


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
        raise error(404, f"Task {task_id} not found")
    return row_to_task(row)


@app.post("/tasks", status_code=201, summary="Create task")
async def create_task(payload: TaskCreate):
    """Create a new task with done=false and current timestamps."""
    if not payload.title or not payload.title.strip():
        raise error(400, "Title is required.")

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
        raise error(400, "At least one field to update is required.")
    if payload.title is not None and not payload.title.strip():
        raise error(400, "Title cannot be empty.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, done FROM tasks WHERE id = %s",
                (task_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise error(404, f"Task {task_id} not found")

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
        raise error(404, f"Task {task_id} not found")
    return Response(status_code=204)
