from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ------------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------------

openapi_tags = [
    {"name": "System", "description": "Health and diagnostics."},
    {"name": "Habits", "description": "CRUD operations for habits."},
    {"name": "Completions", "description": "Daily completion markers (toggle/set/unset)."},
    {"name": "Statistics", "description": "Aggregated analytics such as streaks and completion counts."},
]

app = FastAPI(
    title="Habit Tracker Backend API",
    description=(
        "REST API for the Habit Tracker Android app.\n\n"
        "Backed by a shared SQLite database file owned by the database container.\n\n"
        "Notes for Android emulator/device:\n"
        "- CORS is configured permissively to support emulator/device development.\n"
        "- Prefer using the backend container URL and port configured in your environment."
    ),
    version="1.0.0",
    openapi_tags=openapi_tags,
)

# Keep permissive CORS to remain compatible with emulator/device usage
# (and any web preview tooling). If later tightening is required, do it
# via env-driven allowlist in one place.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

# Canonical DB path as referenced by the database container.
_DEFAULT_DB_PATH = "/home/kavia/workspace/code-generation/habit-tracker-pro-330007-330023/database/myapp.db"


def _get_db_path() -> str:
    """
    Resolve the SQLite DB path.

    Precedence:
      1) SQLITE_DB env var (provided by the database container contract)
      2) canonical path from db_connection.txt in the database container (fallback)
      3) hard-coded default path (template fallback)

    This is intentionally centralized so all DB access uses one deterministic path.
    """
    env_path = os.environ.get("SQLITE_DB")
    if env_path:
        return env_path

    # best-effort read from database container connection reference, if present
    db_connection_txt = "/home/kavia/workspace/code-generation/habit-tracker-pro-330007-330023/database/db_connection.txt"
    try:
        if os.path.exists(db_connection_txt):
            with open(db_connection_txt, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().lower().startswith("# file path:"):
                        _, value = line.split(":", 1)
                        candidate = value.strip()
                        if candidate:
                            return candidate
    except Exception:
        # Never hard-fail configuration parsing here; API will fail later with DB open error if path is invalid.
        pass

    return _DEFAULT_DB_PATH


# ------------------------------------------------------------------------------
# Errors / helpers
# ------------------------------------------------------------------------------


class ApiErrorResponse(BaseModel):
    """Standard API error envelope."""

    detail: str = Field(..., description="Human-readable error message.")


def _parse_iso_date(date_str: str) -> dt.date:
    """Parse YYYY-MM-DD into a date object, raising ValueError for invalid values."""
    return dt.date.fromisoformat(date_str)


def _today_iso() -> str:
    """Return today's date in YYYY-MM-DD (UTC-date; SQLite stored as date string)."""
    # Using UTC date reduces timezone ambiguity across device/server.
    return dt.datetime.utcnow().date().isoformat()


def _row_to_habit(row: sqlite3.Row) -> "Habit":
    return Habit(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        color=row["color"],
        icon=row["icon"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived=bool(row["archived"]),
    )


def _row_to_completion(row: sqlite3.Row) -> "HabitCompletion":
    return HabitCompletion(
        id=row["id"],
        habit_id=row["habit_id"],
        completed_date=row["completed_date"],
        created_at=row["created_at"],
    )


@dataclass(frozen=True)
class DbContext:
    """Typed DB context passed into flows (no hidden global reads beyond boundary)."""

    db_path: str


class SqliteDb:
    """
    SQLite adapter.

    Provides a single, consistent I/O boundary:
    - opens connections with required pragmas
    - uses row_factory for named access
    - wraps sqlite3.Error into HTTP-friendly errors at the API boundary
    """

    def __init__(self, ctx: DbContext):
        self._ctx = ctx

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._ctx.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn


def _http_500_db(detail: str, *, exc: Exception | None = None) -> HTTPException:
    # Keep error messages actionable but not overly verbose. Chain cause for server logs.
    if exc is not None:
        detail = f"{detail} ({type(exc).__name__})"
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


def _http_404(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _http_400(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------


class HabitCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="Habit display name.")
    description: Optional[str] = Field(None, max_length=1000, description="Optional habit description.")
    color: Optional[str] = Field(None, max_length=32, description="Optional color (e.g., hex '#3b82f6').")
    icon: Optional[str] = Field(None, max_length=64, description="Optional icon key (e.g., 'water').")


class HabitUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200, description="Updated name.")
    description: Optional[str] = Field(None, max_length=1000, description="Updated description.")
    color: Optional[str] = Field(None, max_length=32, description="Updated color.")
    icon: Optional[str] = Field(None, max_length=64, description="Updated icon key.")
    archived: Optional[bool] = Field(None, description="Archive/unarchive habit.")


class Habit(BaseModel):
    id: int = Field(..., description="Habit identifier.")
    name: str = Field(..., description="Habit display name.")
    description: Optional[str] = Field(None, description="Optional habit description.")
    color: Optional[str] = Field(None, description="Optional color.")
    icon: Optional[str] = Field(None, description="Optional icon key.")
    created_at: str = Field(..., description="Creation timestamp (SQLite CURRENT_TIMESTAMP).")
    updated_at: str = Field(..., description="Updated timestamp (SQLite CURRENT_TIMESTAMP).")
    archived: bool = Field(..., description="Whether habit is archived.")


class HabitCompletion(BaseModel):
    id: int = Field(..., description="Completion identifier.")
    habit_id: int = Field(..., description="Habit identifier.")
    completed_date: str = Field(..., description="Completed date as ISO YYYY-MM-DD.")
    created_at: str = Field(..., description="Creation timestamp (SQLite CURRENT_TIMESTAMP).")


class CompletionSetRequest(BaseModel):
    habit_id: int = Field(..., description="Habit identifier.")
    date: str = Field(..., description="ISO date YYYY-MM-DD.")
    completed: bool = Field(..., description="If true ensure completion exists, else ensure it's removed.")


class CompletionToggleRequest(BaseModel):
    habit_id: int = Field(..., description="Habit identifier.")
    date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD. Defaults to today's UTC date if omitted.")


class CompletionToggleResponse(BaseModel):
    habit_id: int = Field(..., description="Habit identifier.")
    date: str = Field(..., description="ISO date YYYY-MM-DD.")
    completed: bool = Field(..., description="New completion state after toggle.")


class HabitStats(BaseModel):
    habit_id: int = Field(..., description="Habit identifier.")
    total_completions: int = Field(..., description="Total number of completion days recorded.")
    current_streak: int = Field(..., description="Current consecutive-day streak ending at 'as_of' date if completed.")
    best_streak: int = Field(..., description="Maximum consecutive-day streak observed.")
    last_completed_date: Optional[str] = Field(None, description="Most recent completion date, if any.")


class SummaryStats(BaseModel):
    as_of: str = Field(..., description="ISO date YYYY-MM-DD the summary is computed for.")
    active_habits: int = Field(..., description="Count of non-archived habits.")
    completed_today: int = Field(..., description="Number of non-archived habits completed on 'as_of'.")
    completion_rate_today: float = Field(..., ge=0.0, le=1.0, description="completed_today / active_habits (0 if none).")


# ------------------------------------------------------------------------------
# Flows (reusable orchestration)
# ------------------------------------------------------------------------------


class HabitFlows:
    """Use-case flows for habits."""

    def __init__(self, db: SqliteDb):
        self._db = db

    def list_habits(self, *, include_archived: bool) -> List[Habit]:
        with self._db.connect() as conn:
            if include_archived:
                cur = conn.execute("SELECT * FROM habits ORDER BY archived ASC, id ASC")
            else:
                cur = conn.execute("SELECT * FROM habits WHERE archived = 0 ORDER BY id ASC")
            return [_row_to_habit(r) for r in cur.fetchall()]

    def get_habit(self, habit_id: int) -> Habit:
        with self._db.connect() as conn:
            cur = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,))
            row = cur.fetchone()
            if not row:
                raise _http_404(f"Habit {habit_id} not found")
            return _row_to_habit(row)

    def create_habit(self, req: HabitCreate) -> Habit:
        with self._db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO habits (name, description, color, icon, created_at, updated_at, archived)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                """.strip(),
                (req.name, req.description, req.color, req.icon),
            )
            habit_id = int(cur.lastrowid)
            cur2 = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,))
            return _row_to_habit(cur2.fetchone())

    def update_habit(self, habit_id: int, req: HabitUpdate) -> Habit:
        # Invariant: updated_at always refreshed on update.
        with self._db.connect() as conn:
            cur = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,))
            if not cur.fetchone():
                raise _http_404(f"Habit {habit_id} not found")

            # Build deterministic update statement
            fields: List[str] = []
            params: List[Any] = []
            if req.name is not None:
                fields.append("name = ?")
                params.append(req.name)
            if req.description is not None:
                fields.append("description = ?")
                params.append(req.description)
            if req.color is not None:
                fields.append("color = ?")
                params.append(req.color)
            if req.icon is not None:
                fields.append("icon = ?")
                params.append(req.icon)
            if req.archived is not None:
                fields.append("archived = ?")
                params.append(1 if req.archived else 0)

            fields.append("updated_at = CURRENT_TIMESTAMP")

            params.append(habit_id)
            conn.execute(f"UPDATE habits SET {', '.join(fields)} WHERE id = ?", tuple(params))

            cur2 = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,))
            return _row_to_habit(cur2.fetchone())

    def delete_habit(self, habit_id: int) -> None:
        with self._db.connect() as conn:
            cur = conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
            if cur.rowcount == 0:
                raise _http_404(f"Habit {habit_id} not found")


class CompletionFlows:
    """Use-case flows for daily completions."""

    def __init__(self, db: SqliteDb):
        self._db = db

    def list_completions(
        self,
        *,
        habit_id: Optional[int],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> List[HabitCompletion]:
        if start_date is not None:
            _parse_iso_date(start_date)
        if end_date is not None:
            _parse_iso_date(end_date)

        where: List[str] = []
        params: List[Any] = []

        if habit_id is not None:
            where.append("habit_id = ?")
            params.append(habit_id)
        if start_date is not None:
            where.append("completed_date >= ?")
            params.append(start_date)
        if end_date is not None:
            where.append("completed_date <= ?")
            params.append(end_date)

        sql = "SELECT * FROM habit_completions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY completed_date ASC, id ASC"

        with self._db.connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return [_row_to_completion(r) for r in cur.fetchall()]

    def set_completion(self, req: CompletionSetRequest) -> bool:
        # Returns final state: completed?
        _parse_iso_date(req.date)
        with self._db.connect() as conn:
            # validate habit exists
            cur = conn.execute("SELECT 1 FROM habits WHERE id = ?", (req.habit_id,))
            if not cur.fetchone():
                raise _http_404(f"Habit {req.habit_id} not found")

            if req.completed:
                # insert if absent
                conn.execute(
                    """
                    INSERT OR IGNORE INTO habit_completions (habit_id, completed_date, created_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    """.strip(),
                    (req.habit_id, req.date),
                )
                return True

            # else remove if present
            conn.execute(
                "DELETE FROM habit_completions WHERE habit_id = ? AND completed_date = ?",
                (req.habit_id, req.date),
            )
            return False

    def toggle_completion(self, req: CompletionToggleRequest) -> CompletionToggleResponse:
        date = req.date or _today_iso()
        _parse_iso_date(date)

        with self._db.connect() as conn:
            cur = conn.execute("SELECT 1 FROM habits WHERE id = ?", (req.habit_id,))
            if not cur.fetchone():
                raise _http_404(f"Habit {req.habit_id} not found")

            cur2 = conn.execute(
                "SELECT id FROM habit_completions WHERE habit_id = ? AND completed_date = ?",
                (req.habit_id, date),
            )
            row = cur2.fetchone()
            if row:
                conn.execute(
                    "DELETE FROM habit_completions WHERE habit_id = ? AND completed_date = ?",
                    (req.habit_id, date),
                )
                return CompletionToggleResponse(habit_id=req.habit_id, date=date, completed=False)

            conn.execute(
                """
                INSERT INTO habit_completions (habit_id, completed_date, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """.strip(),
                (req.habit_id, date),
            )
            return CompletionToggleResponse(habit_id=req.habit_id, date=date, completed=True)


class StatisticsFlows:
    """Use-case flows for statistics and streak computation."""

    def __init__(self, db: SqliteDb):
        self._db = db

    @staticmethod
    def _compute_streaks_from_sorted_dates(sorted_dates: List[dt.date], *, as_of: dt.date) -> Tuple[int, int]:
        """
        Compute (current_streak, best_streak).

        Invariants:
          - sorted_dates must be strictly ascending unique dates.
          - current_streak counts consecutive days ending at as_of, but only if as_of is completed.
        """
        if not sorted_dates:
            return 0, 0

        best = 1
        run = 1
        for i in range(1, len(sorted_dates)):
            if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
                run += 1
            else:
                best = max(best, run)
                run = 1
        best = max(best, run)

        completed_set = set(sorted_dates)
        if as_of not in completed_set:
            current = 0
        else:
            current = 1
            d = as_of
            while (d - dt.timedelta(days=1)) in completed_set:
                current += 1
                d = d - dt.timedelta(days=1)

        return current, best

    def habit_stats(self, habit_id: int, *, as_of: str) -> HabitStats:
        as_of_date = _parse_iso_date(as_of)

        with self._db.connect() as conn:
            cur = conn.execute("SELECT 1 FROM habits WHERE id = ?", (habit_id,))
            if not cur.fetchone():
                raise _http_404(f"Habit {habit_id} not found")

            cur2 = conn.execute(
                "SELECT completed_date FROM habit_completions WHERE habit_id = ? ORDER BY completed_date ASC",
                (habit_id,),
            )
            date_rows = [r["completed_date"] for r in cur2.fetchall()]
            dates = [dt.date.fromisoformat(s) for s in date_rows]

            total = len(dates)
            last_completed = date_rows[-1] if date_rows else None
            current, best = self._compute_streaks_from_sorted_dates(dates, as_of=as_of_date)

            return HabitStats(
                habit_id=habit_id,
                total_completions=total,
                current_streak=current,
                best_streak=best,
                last_completed_date=last_completed,
            )

    def summary_stats(self, *, as_of: str) -> SummaryStats:
        as_of_date = _parse_iso_date(as_of)
        as_of_iso = as_of_date.isoformat()

        with self._db.connect() as conn:
            cur = conn.execute("SELECT COUNT(1) AS c FROM habits WHERE archived = 0")
            active = int(cur.fetchone()["c"])

            cur2 = conn.execute(
                """
                SELECT COUNT(DISTINCT h.id) AS c
                FROM habits h
                JOIN habit_completions hc ON hc.habit_id = h.id
                WHERE h.archived = 0 AND hc.completed_date = ?
                """.strip(),
                (as_of_iso,),
            )
            completed_today = int(cur2.fetchone()["c"])

            rate = (completed_today / active) if active > 0 else 0.0
            return SummaryStats(
                as_of=as_of_iso,
                active_habits=active,
                completed_today=completed_today,
                completion_rate_today=rate,
            )


def _flows() -> Tuple[HabitFlows, CompletionFlows, StatisticsFlows]:
    """
    Create flow singletons per request.

    This is kept as a single factory so all endpoints share the same adapter and
    config resolution path.
    """
    ctx = DbContext(db_path=_get_db_path())
    db = SqliteDb(ctx)
    return HabitFlows(db), CompletionFlows(db), StatisticsFlows(db)


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------


# PUBLIC_INTERFACE
@app.get(
    "/",
    tags=["System"],
    summary="Health check",
    description="Simple health check endpoint.",
    operation_id="health_check",
)
def health_check() -> Dict[str, str]:
    """Health check endpoint used for service liveness."""
    return {"message": "Healthy"}


# PUBLIC_INTERFACE
@app.get(
    "/system/db",
    tags=["System"],
    summary="DB diagnostics",
    description="Return the resolved DB path and basic table existence checks.",
    operation_id="system_db_diagnostics",
    responses={200: {"description": "DB diagnostics"}, 500: {"model": ApiErrorResponse}},
)
def db_diagnostics() -> Dict[str, Any]:
    """
    Provide basic DB diagnostics to help debug cross-container integration.

    Returns:
      - db_path resolved
      - whether required tables exist
      - current PRAGMA user_version
    """
    db_path = _get_db_path()
    try:
        ctx = DbContext(db_path=db_path)
        db = SqliteDb(ctx)
        with db.connect() as conn:
            cur = conn.execute("PRAGMA user_version")
            user_version = int(cur.fetchone()[0])
            cur2 = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('habits','habit_completions')"
            )
            present = sorted([r[0] for r in cur2.fetchall()])
            return {
                "db_path": db_path,
                "user_version": user_version,
                "required_tables_present": present,
                "ok": set(present) == {"habits", "habit_completions"},
            }
    except sqlite3.Error as e:
        raise _http_500_db("Failed to query SQLite DB. Check SQLITE_DB path and permissions.", exc=e) from e


# PUBLIC_INTERFACE
@app.get(
    "/habits",
    tags=["Habits"],
    summary="List habits",
    description="List habits. By default returns only non-archived habits.",
    operation_id="list_habits",
    response_model=List[Habit],
    responses={500: {"model": ApiErrorResponse}},
)
def list_habits(
    include_archived: bool = Query(
        False, description="If true include archived habits in the response."
    ),
) -> List[Habit]:
    """List habits."""
    habits, _, _ = _flows()
    try:
        return habits.list_habits(include_archived=include_archived)
    except sqlite3.Error as e:
        raise _http_500_db("Failed to list habits.", exc=e) from e


# PUBLIC_INTERFACE
@app.post(
    "/habits",
    tags=["Habits"],
    summary="Create habit",
    description="Create a new habit.",
    operation_id="create_habit",
    response_model=Habit,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
)
def create_habit(req: HabitCreate) -> Habit:
    """Create a new habit."""
    habits, _, _ = _flows()
    try:
        return habits.create_habit(req)
    except sqlite3.IntegrityError as e:
        raise _http_400("Invalid habit payload.") from e
    except sqlite3.Error as e:
        raise _http_500_db("Failed to create habit.", exc=e) from e


# PUBLIC_INTERFACE
@app.get(
    "/habits/{habit_id}",
    tags=["Habits"],
    summary="Get habit",
    description="Get a habit by id.",
    operation_id="get_habit",
    response_model=Habit,
    responses={404: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
)
def get_habit(habit_id: int) -> Habit:
    """Get a habit by its id."""
    habits, _, _ = _flows()
    try:
        return habits.get_habit(habit_id)
    except HTTPException:
        raise
    except sqlite3.Error as e:
        raise _http_500_db("Failed to get habit.", exc=e) from e


# PUBLIC_INTERFACE
@app.put(
    "/habits/{habit_id}",
    tags=["Habits"],
    summary="Update habit",
    description="Update habit fields (partial update supported).",
    operation_id="update_habit",
    response_model=Habit,
    responses={404: {"model": ApiErrorResponse}, 400: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
)
def update_habit(habit_id: int, req: HabitUpdate) -> Habit:
    """Update a habit."""
    habits, _, _ = _flows()
    try:
        return habits.update_habit(habit_id, req)
    except HTTPException:
        raise
    except sqlite3.IntegrityError as e:
        raise _http_400("Invalid habit update payload.") from e
    except sqlite3.Error as e:
        raise _http_500_db("Failed to update habit.", exc=e) from e


# PUBLIC_INTERFACE
@app.delete(
    "/habits/{habit_id}",
    tags=["Habits"],
    summary="Delete habit",
    description="Delete a habit. Completions are deleted automatically via foreign key cascade.",
    operation_id="delete_habit",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
)
def delete_habit(habit_id: int) -> None:
    """Delete a habit."""
    habits, _, _ = _flows()
    try:
        habits.delete_habit(habit_id)
        return None
    except HTTPException:
        raise
    except sqlite3.Error as e:
        raise _http_500_db("Failed to delete habit.", exc=e) from e


# PUBLIC_INTERFACE
@app.get(
    "/completions",
    tags=["Completions"],
    summary="List completions",
    description="List completion entries, optionally filtered by habit and date range.",
    operation_id="list_completions",
    response_model=List[HabitCompletion],
    responses={400: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
)
def list_completions(
    habit_id: Optional[int] = Query(None, description="Filter by habit id."),
    start_date: Optional[str] = Query(None, description="Inclusive ISO date YYYY-MM-DD."),
    end_date: Optional[str] = Query(None, description="Inclusive ISO date YYYY-MM-DD."),
) -> List[HabitCompletion]:
    """List completions with optional filters."""
    _, completions, _ = _flows()
    try:
        return completions.list_completions(habit_id=habit_id, start_date=start_date, end_date=end_date)
    except ValueError as e:
        raise _http_400(str(e)) from e
    except sqlite3.Error as e:
        raise _http_500_db("Failed to list completions.", exc=e) from e


# PUBLIC_INTERFACE
@app.post(
    "/completions/set",
    tags=["Completions"],
    summary="Set completion state",
    description="Ensure a completion exists (completed=true) or is removed (completed=false) for a habit/date.",
    operation_id="set_completion",
    response_model=Dict[str, Any],
    responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
)
def set_completion(req: CompletionSetRequest) -> Dict[str, Any]:
    """Set completion state for a habit on a given date."""
    _, completions, _ = _flows()
    try:
        completed = completions.set_completion(req)
        return {"habit_id": req.habit_id, "date": req.date, "completed": completed}
    except ValueError as e:
        raise _http_400(str(e)) from e
    except HTTPException:
        raise
    except sqlite3.Error as e:
        raise _http_500_db("Failed to set completion.", exc=e) from e


# PUBLIC_INTERFACE
@app.post(
    "/completions/toggle",
    tags=["Completions"],
    summary="Toggle completion state",
    description="Toggle completion for a habit on a date (defaults to today's UTC date).",
    operation_id="toggle_completion",
    response_model=CompletionToggleResponse,
    responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
)
def toggle_completion(req: CompletionToggleRequest) -> CompletionToggleResponse:
    """Toggle completion for a habit/date."""
    _, completions, _ = _flows()
    try:
        return completions.toggle_completion(req)
    except ValueError as e:
        raise _http_400(str(e)) from e
    except HTTPException:
        raise
    except sqlite3.IntegrityError as e:
        # Unique constraint could theoretically trigger under race; treat as completed.
        raise _http_400("Could not toggle completion due to a data constraint.") from e
    except sqlite3.Error as e:
        raise _http_500_db("Failed to toggle completion.", exc=e) from e


# PUBLIC_INTERFACE
@app.get(
    "/stats/habits/{habit_id}",
    tags=["Statistics"],
    summary="Habit statistics",
    description="Get stats for a habit including streaks and total completions.",
    operation_id="habit_stats",
    response_model=HabitStats,
    responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
)
def habit_stats(
    habit_id: int,
    as_of: str = Query(
        default_factory=_today_iso,
        description="Compute current streak as of this ISO date YYYY-MM-DD. Defaults to today's UTC date.",
    ),
) -> HabitStats:
    """Compute statistics for a habit."""
    _, _, stats = _flows()
    try:
        return stats.habit_stats(habit_id, as_of=as_of)
    except ValueError as e:
        raise _http_400(str(e)) from e
    except HTTPException:
        raise
    except sqlite3.Error as e:
        raise _http_500_db("Failed to compute habit stats.", exc=e) from e


# PUBLIC_INTERFACE
@app.get(
    "/stats/summary",
    tags=["Statistics"],
    summary="Summary statistics",
    description="Get summary stats for the day: active habits, completed today, completion rate.",
    operation_id="summary_stats",
    response_model=SummaryStats,
    responses={400: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
)
def summary_stats(
    as_of: str = Query(
        default_factory=_today_iso,
        description="ISO date YYYY-MM-DD. Defaults to today's UTC date.",
    ),
) -> SummaryStats:
    """Compute summary statistics for a given day."""
    _, _, stats = _flows()
    try:
        return stats.summary_stats(as_of=as_of)
    except ValueError as e:
        raise _http_400(str(e)) from e
    except sqlite3.Error as e:
        raise _http_500_db("Failed to compute summary stats.", exc=e) from e


# PUBLIC_INTERFACE
@app.get(
    "/docs/usage/android",
    tags=["System"],
    summary="Android client usage notes",
    description="Short notes for consuming this API from Android (Retrofit) and emulator/device considerations.",
    operation_id="android_usage_notes",
    response_model=Dict[str, str],
)
def android_usage_notes() -> Dict[str, str]:
    """Provide lightweight documentation for Android consumption."""
    return {
        "base_url_note": (
            "Use the backend container URL shown in the environment (or Kavia running_containers). "
            "From an Android emulator, '10.0.2.2' maps to the host machine; for real devices, use the host LAN IP."
        ),
        "date_format": "Dates are ISO strings 'YYYY-MM-DD'. If date omitted in toggle, server uses today's UTC date.",
        "endpoints": (
            "Habits: GET/POST /habits, GET/PUT/DELETE /habits/{id}. "
            "Completions: GET /completions, POST /completions/set, POST /completions/toggle. "
            "Stats: GET /stats/habits/{id}, GET /stats/summary."
        ),
    }
