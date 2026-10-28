import secrets
from datetime import datetime
from typing import Literal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.security import HttpBearer

from .models import Task

PriorityValue = Task.Priority | Literal[""]
StatusValue = Task.Status | Literal[""]
EffortValue = Task.Effort | Literal[""]

api = NinjaAPI(title="Tasks", urls_namespace="tasks")

_TEXT_FIELDS = (
    "priority",
    "description",
    "task_deepdive_link",
    "ticket_id",
    "effort",
    "status",
    "assignee",
)


class TokenAuth(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str):
        expected = settings.TASKS_API_TOKEN
        if expected and secrets.compare_digest(token, expected):
            return token
        return None


class TaskIn(Schema):
    email_address: str
    task_title: str
    timestamp: datetime | None = None
    priority: PriorityValue = ""
    description: str = ""
    task_deepdive_link: str = ""
    ticket_id: str = ""
    effort: EffortValue = ""
    status: StatusValue = ""
    assignee: str = ""


class TaskPatch(Schema):
    email_address: str | None = None
    task_title: str | None = None
    timestamp: datetime | None = None
    priority: PriorityValue | None = None
    description: str | None = None
    task_deepdive_link: str | None = None
    ticket_id: str | None = None
    effort: EffortValue | None = None
    status: StatusValue | None = None
    assignee: str | None = None


class TaskOut(Schema):
    id: int
    timestamp: datetime
    email_address: str
    task_title: str
    priority: str
    description: str
    task_deepdive_link: str
    ticket_id: str
    effort: str
    status: str
    assignee: str


def _validation_message(exc: ValidationError) -> str:
    if hasattr(exc, "message_dict"):
        parts = []
        for field, messages in exc.message_dict.items():
            joined = ", ".join(messages)
            parts.append(f"{field}: {joined}")
        return "; ".join(parts)
    return "; ".join(exc.messages)


def _apply(task: Task, data: dict) -> Task:
    for key, value in data.items():
        if value is None and key in _TEXT_FIELDS:
            value = ""
        if key == "timestamp" and value is None:
            continue
        setattr(task, key, value)
    try:
        task.full_clean()
    except ValidationError as exc:
        raise HttpError(422, _validation_message(exc)) from exc
    task.save()
    return task


@api.get("/tasks", response=list[TaskOut], auth=TokenAuth())
def list_tasks(
    request,
    status: str | None = None,
    assignee: str | None = None,
    priority: str | None = None,
):
    tasks = Task.objects.all()
    if status is not None:
        tasks = tasks.filter(status=status)
    if assignee is not None:
        tasks = tasks.filter(assignee=assignee)
    if priority is not None:
        tasks = tasks.filter(priority=priority)
    return list(tasks)


@api.post("/tasks", response={201: TaskOut}, auth=TokenAuth())
def create_task(request, payload: TaskIn):
    task = _apply(Task(), payload.model_dump(exclude_unset=True))
    return 201, task


@api.get("/tasks/{task_id}", response=TaskOut, auth=TokenAuth())
def get_task(request, task_id: int):
    return get_object_or_404(Task, pk=task_id)


@api.patch("/tasks/{task_id}", response=TaskOut, auth=TokenAuth())
def patch_task(request, task_id: int, payload: TaskPatch):
    task = get_object_or_404(Task, pk=task_id)
    return _apply(task, payload.model_dump(exclude_unset=True))
