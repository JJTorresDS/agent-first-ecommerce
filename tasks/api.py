import secrets
import uuid
from datetime import date as Date
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.security import HttpBearer

from accounts.models import Organization

from .models import CashflowMovement, Task

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


def _organization_or_404(organization_id: str) -> Organization:
    try:
        parsed = uuid.UUID(str(organization_id))
    except ValueError:
        raise HttpError(422, "organization: Enter a valid id.") from None
    return get_object_or_404(Organization, pk=parsed)


class TaskIn(Schema):
    organization: str
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
    organization: str
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
    organization: str
    timestamp: datetime

    @staticmethod
    def resolve_organization(obj):
        return str(obj.organization_id)
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
    organization: str,
    status: str | None = None,
    assignee: str | None = None,
    priority: str | None = None,
):
    tasks = Task.objects.filter(organization=_organization_or_404(organization))
    if status is not None:
        tasks = tasks.filter(status=status)
    if assignee is not None:
        tasks = tasks.filter(assignee=assignee)
    if priority is not None:
        tasks = tasks.filter(priority=priority)
    return list(tasks)


@api.post("/tasks", response={201: TaskOut}, auth=TokenAuth())
def create_task(request, payload: TaskIn):
    data = payload.model_dump(exclude_unset=True)
    data["organization"] = _organization_or_404(data.pop("organization"))
    task = _apply(Task(), data)
    return 201, task


@api.get("/tasks/{task_id}", response=TaskOut, auth=TokenAuth())
def get_task(request, task_id: int, organization: str):
    return get_object_or_404(
        Task,
        pk=task_id,
        organization=_organization_or_404(organization),
    )


@api.patch("/tasks/{task_id}", response=TaskOut, auth=TokenAuth())
def patch_task(request, task_id: int, payload: TaskPatch):
    data = payload.model_dump(exclude_unset=True)
    task = get_object_or_404(
        Task,
        pk=task_id,
        organization=_organization_or_404(data.pop("organization")),
    )
    return _apply(task, data)


_MOVEMENT_TEXT = (
    "counterparty_name",
    "description",
    "source_file_id",
    "receipt_id",
)


class CashflowMovementIn(Schema):
    organization: str
    uploader_email: str
    transaction_date: Date
    category: str
    sub_category: str | None = None
    amount: Decimal
    currency: str
    exchange_rate_at_purchase: Decimal | None = None
    counterparty_name: str = ""
    description: str = ""
    source_file_id: str = ""
    receipt_id: Optional[str] = None
    white: bool = False


class CashflowMovementPatch(Schema):
    organization: str
    uploader_email: str | None = None
    transaction_date: Date | None = None
    category: str | None = None
    sub_category: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    exchange_rate_at_purchase: Decimal | None = None
    counterparty_name: str | None = None
    description: str | None = None
    source_file_id: str | None = None
    receipt_id: str | None = None
    white: bool | None = None


class CashflowMovementOut(Schema):
    id: str
    organization: str
    uploader_email: str

    @staticmethod
    def resolve_organization(obj):
        return str(obj.organization_id)
    transaction_date: Date
    category: str
    sub_category: str | None
    amount: Decimal
    currency: str
    exchange_rate_at_purchase: Decimal | None
    counterparty_name: str
    description: str
    source_file_id: str
    receipt_id: str
    white: bool
    created_at: datetime
    updated_at: datetime


def _apply_movement(movement: CashflowMovement, data: dict) -> CashflowMovement:
    for key, value in data.items():
        if value is None and key in _MOVEMENT_TEXT:
            value = ""
        if value is None and key not in {
            "exchange_rate_at_purchase",
            "sub_category",
        }:
            continue
        setattr(movement, key, value)
    try:
        movement.full_clean()
    except ValidationError as exc:
        raise HttpError(422, _validation_message(exc)) from exc
    movement.save()
    return movement


@api.get("/cashflow-movements", response=list[CashflowMovementOut], auth=TokenAuth())
def list_cashflow_movements(
    request,
    organization: str,
    category: str | None = None,
    currency: str | None = None,
):
    movements = CashflowMovement.objects.filter(
        organization=_organization_or_404(organization)
    )
    if category is not None:
        movements = movements.filter(category=category)
    if currency is not None:
        movements = movements.filter(currency=currency)
    return list(movements)


@api.post("/cashflow-movements", response={201: CashflowMovementOut}, auth=TokenAuth())
def create_cashflow_movement(request, payload: CashflowMovementIn):
    data = payload.model_dump(exclude_unset=True)
    data["organization"] = _organization_or_404(data.pop("organization"))
    movement = _apply_movement(CashflowMovement(), data)
    return 201, movement


def _upsert_movement(source_file_id: str, data: dict) -> tuple[CashflowMovement, bool]:
    organization = data["organization"]
    with transaction.atomic():
        movement = (
            CashflowMovement.objects.select_for_update()
            .filter(source_file_id=source_file_id, organization=organization)
            .first()
        )
        created = movement is None
        if created:
            movement = CashflowMovement()
        return _apply_movement(movement, data), created


@api.put(
    "/cashflow-movements/by-source/{source_file_id}",
    response={200: CashflowMovementOut, 201: CashflowMovementOut},
    auth=TokenAuth(),
)
def upsert_cashflow_movement(request, source_file_id: str, payload: CashflowMovementIn):
    if not source_file_id.strip():
        raise HttpError(422, "source_file_id: This field is required.")
    data = payload.model_dump(exclude_unset=True)
    data["organization"] = _organization_or_404(data.pop("organization"))
    data["source_file_id"] = source_file_id
    try:
        movement, created = _upsert_movement(source_file_id, data)
    except IntegrityError:
        with transaction.atomic():
            movement = (
                CashflowMovement.objects.select_for_update()
                .filter(
                    source_file_id=source_file_id,
                    organization=data["organization"],
                )
                .first()
            )
            if movement is None:
                raise
            movement = _apply_movement(movement, data)
        created = False
    return (201 if created else 200), movement


@api.get(
    "/cashflow-movements/{movement_id}",
    response=CashflowMovementOut,
    auth=TokenAuth(),
)
def get_cashflow_movement(request, movement_id: str, organization: str):
    return get_object_or_404(
        CashflowMovement,
        pk=movement_id,
        organization=_organization_or_404(organization),
    )


@api.patch(
    "/cashflow-movements/{movement_id}",
    response=CashflowMovementOut,
    auth=TokenAuth(),
)
def patch_cashflow_movement(request, movement_id: str, payload: CashflowMovementPatch):
    data = payload.model_dump(exclude_unset=True)
    movement = get_object_or_404(
        CashflowMovement,
        pk=movement_id,
        organization=_organization_or_404(data.pop("organization")),
    )
    return _apply_movement(movement, data)
