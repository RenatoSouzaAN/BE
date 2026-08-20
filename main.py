import os
from typing import Annotated
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import psycopg

from datetime import datetime
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic.main import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
from supabase_auth.errors import AuthApiError

app = FastAPI()

security = HTTPBearer()

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")

if not url or not key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(url, key)
print("Server running and connected to Supabase")

class UserCreate(BaseModel):
    email: str
    password: str

class UserLogin(BaseModel):
    email: str
    password: str

class Task(BaseModel):
    id: int
    title: str
    done: bool

class TaskCreate(BaseModel):
    title: str | None = None

class TaskUpdate(BaseModel):
    title: str | None = None
    done: bool | None = None

SEED_TASKS = [
    {"id": 1, "title": "Task 1", "done": True, "created_at": datetime.now(), "updated_at": datetime.now()},
    {"id": 2, "title": "Task 2", "done": False, "created_at": datetime.now(), "updated_at": datetime.now()},
    {"id": 3, "title": "Task 3", "done": True, "created_at": datetime.now(), "updated_at": datetime.now()},
]

def init_db():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS tasks (id SERIAL PRIMARY KEY, title TEXT, done BOOLEAN, created_at TEXT, updated_at TEXT)")
    
            cur.execute("SELECT COUNT(*) FROM tasks")
            count = cur.fetchone()[0]
            if count == 0:
                for task in SEED_TASKS:
                    cur.execute("INSERT INTO tasks (title, done, created_at, updated_at) VALUES (%s, %s, %s, %s)", (task["title"], task["done"], task["created_at"], task["updated_at"]))
            conn.commit()

init_db()

def row_to_task(row):
    return {
        "id": row[0],
        "title": row[1],
        "done": bool(row[2]),
    }

async def get_current_user(credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)]):
    """Get the current user from the request headers."""
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Access token required")

    token = credentials.credentials
    if not token:
        raise HTTPException(status_code=401, detail="Access token required")

    try:
        response = supabase.auth.get_user(token)
    except AuthApiError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if response.user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return response.user

@app.get("/")
async def root():
    """
    Root endpoint for the Task API.
    Returns a dictionary with the name, version, and endpoints of the API.
    """
    return {"name": "Task API", "version": "1.0", "endpoints": ["/tasks"]}

@app.get("/health")
async def health():
    """Health check endpoint.
    Returns a dictionary with the status of the API.
    """
    return {"status": "ok"}

@app.post("/auth/signup", status_code=201)
async def signup(user: UserCreate):
    """Sign up a new user."""
    if not user or not user.email or not user.password:
        return JSONResponse(status_code=400, content={"error": "Email and password are required"})

    response = supabase.auth.sign_up({
        "email": user.email,
        "password": user.password,
    })
    return response.user

@app.post("/auth/login", status_code=200)
async def login(user: UserLogin):
    """Sign in a user."""
    if not user or not user.email or not user.password:
        return JSONResponse(status_code=400, content={"error": "Email and password are required"})
    try:
        response = supabase.auth.sign_in_with_password({
            "email": user.email,
            "password": user.password,
        })
    except AuthApiError:
        return JSONResponse(status_code=401, content={"error": "Invalid login credentials"})

    if response.session is None:
        return JSONResponse(status_code=401, content={"error": "Invalid login credentials"})
    
    return {
        "access_token": response.session.access_token,
        "refresh_token": response.session.refresh_token,
    }

@app.get("/public/info")
async def get_public_info():
    """Get public information."""
    return JSONResponse(status_code=200, content={"message": "Welcome, Stranger! This info is public."})

@app.get("/protected/profile")
async def get_protected_info(user=Depends(get_current_user)):
    """Get protected profile information."""
    return JSONResponse(
        status_code=200,
        content={
            "id": user.id,
            "email": user.email,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
    )

@app.get("/protected/dashboard")
async def get_protected_dashboard(user=Depends(get_current_user)):
    """Get the protected dashboard."""
    return JSONResponse(
        status_code=200,
        content={
            "id": user.id,
            "email": user.email,
        },
    )

@app.post("/auth/logout", status_code=204)
async def logout(user=Depends(get_current_user)):
    """Logout a user."""
    try:
        supabase.auth.sign_out()
    except AuthApiError:
        return JSONResponse(status_code=401, content={"error": "Invalid or expired token"})
    return Response(status_code=204)

@app.post("/reset")
async def reset_tasks_list():
    """Reset the tasks list."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE tasks RESTART IDENTITY")
            for task in SEED_TASKS:
                cur.execute("INSERT INTO tasks (title, done, created_at, updated_at) VALUES (%s, %s, %s, %s)", (task["title"], task["done"], task["created_at"], task["updated_at"]))
            conn.commit()
    return JSONResponse(status_code=200, content={"message": "Tasks list reset successfully."})

@app.get("/tasks")
async def get_tasks(done: bool | None = None, search: str | None = None):
    """List all tasks."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            clauses = []
            params = []

            if done is not None:
                clauses.append("done = %s")
                params.append(done)

            if search is not None:
                clauses.append("LOWER(title) LIKE LOWER(%s)")
                params.append(f"%{search}%")

            query = "SELECT * FROM tasks"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)

            cur.execute(query, params)
            filtered_tasks = cur.fetchall()
            return [row_to_task(row) for row in filtered_tasks]

@app.get("/tasks/stats")
async def get_tasks_stats():
    """Get the statistics of the tasks."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE done), COUNT(*) FILTER (WHERE NOT done) FROM tasks")
            total, done_count, pending = cur.fetchone()
    return {
        "total": total or 0,
        "done": done_count or 0,
        "pending": pending or 0,
    }

@app.get("/tasks/{id}")
async def get_tasks_by_id(id: int):
    """Get a task by its ID."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE id = %s", (id,))
            task = cur.fetchone()
            if task:
                return row_to_task(task)
            else:
                return JSONResponse(status_code=404,content={"error": f"Task {id} not found"})

@app.post("/tasks", status_code=201)
async def create_task(task: TaskCreate):
    """Create a new task."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            new_task = None
            if not task.title:
                return JSONResponse(status_code=400, content={"error": "Title is required."})

            cur.execute("INSERT INTO tasks (title, done, created_at, updated_at) VALUES (%s, %s, %s, %s) RETURNING *", (task.title, False, datetime.now(), datetime.now()))
            conn.commit()
            new_task = cur.fetchone()

            return row_to_task(new_task)

@app.put("/tasks/{id}")
async def update_task(id: int, task: TaskUpdate):
    """Update a task by its ID."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE id = %s", (id,))
            currentValue = cur.fetchone()
            if currentValue:
                if task.title is None and task.done is None:
                    return JSONResponse(status_code=400, content={"error": "At least one field to update is required."})

                if task.title is None:
                    task.title = currentValue[1]
                if task.done is None:
                    task.done = currentValue[2]

                cur.execute("UPDATE tasks SET title = %s, done = %s, updated_at = %s WHERE id = %s RETURNING *", (task.title, task.done, datetime.now(), id))
                conn.commit()
                updated_task = cur.fetchone()
                return row_to_task(updated_task)
            else:
                return JSONResponse(status_code=404,content={"error": f"Task {id} not found"})

@app.delete("/tasks/{id}", status_code=204)
async def delete_task_by_id(id: int):
    """Delete a task by its ID."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:   
            cur.execute("DELETE FROM tasks WHERE id = %s", (id,))
            conn.commit()

            if cur.rowcount == 0:
                conn.close()
                return JSONResponse(status_code=404, content={"error": f"Task {id} not found"})
            
            conn.close()
            return Response(status_code=204)
