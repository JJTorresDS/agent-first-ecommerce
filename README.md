---
title: Scent tasks
emoji: 📋
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# Scent tasks

Django is the system of record for one `tasks` table on Neon Postgres. Humans use the admin. A separate agents project creates and updates tasks through the HTTP API. Agents never touch the database.

## Run locally

```bash
uv sync
cp .env.example .env
```

Put your Neon connection string in `DATABASE_URL` in `.env`. Include `sslmode=require`. The pooled Neon host is the one to use.

```bash
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Admin is at http://127.0.0.1:8000/admin/. OpenAPI is at http://127.0.0.1:8000/api/docs.

API calls send `Authorization: Bearer <TASKS_API_TOKEN>` from `.env`.

## Hugging Face Space

Create a Docker Space from this repo. In Space settings, set these secrets:

| Secret | Purpose |
| --- | --- |
| `DATABASE_URL` | Neon connection string, with `sslmode=require`. |
| `SECRET_KEY` | Django secret. Required on the Space. |
| `TASKS_API_TOKEN` | Bearer token for the agents project. |
| `DJANGO_SUPERUSER_USERNAME` | Admin username created on startup. |
| `DJANGO_SUPERUSER_EMAIL` | Admin email created on startup. |
| `DJANGO_SUPERUSER_PASSWORD` | Admin password created on startup. |
| `CSRF_TRUSTED_ORIGINS` | The Space origin, `https://<owner>-<space>.hf.space`. |

`DEBUG` stays off on the Space unless you set it. Tasks live in Neon, so a Space restart does not erase them.
