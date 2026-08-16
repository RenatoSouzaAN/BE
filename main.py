import os
import psycopg

from datetime import datetime
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from pydantic.main import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client

app = FastAPI()

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")

if not url or not key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(url, key)
print("Server running and connected to Supabase")

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
