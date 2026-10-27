# Vimple

Vimple is the backend. Scent is the first tenant. Django is the system of record for one `tasks` table on Neon Postgres. Humans use the admin. A separate agents project creates and updates tasks through the HTTP API. Agents never touch the database.

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

## Render

Connect the GitHub repo in the [Render dashboard](https://dashboard.render.com/blueprint/new) and apply `render.yaml`. The service is a Docker web service named `vimple-backend`.

Paste these two values when Render asks:

- `DATABASE_URL`: the Neon connection string, with `sslmode=require`.
- `CSRF_TRUSTED_ORIGINS`: the service origin only, `https://vimple-backend.onrender.com`, with no path. If Render assigns a different host, use that origin instead.

Render generates `SECRET_KEY` and `TASKS_API_TOKEN`. Copy `TASKS_API_TOKEN` into the agents project. Leave `DEBUG` unset. Leave `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, and `DJANGO_SUPERUSER_PASSWORD` unset. The admin user already in Neon is the login. Startup creates a superuser only when `DJANGO_SUPERUSER_PASSWORD` is set, and it does not change an existing user's password.

After the first deploy, open `/admin/` and call `/api/tasks` with `Authorization: Bearer <TASKS_API_TOKEN>`.

`GET /health/` returns `ok` and does not touch the database. Ping that URL from an outside scheduler every 10 minutes so the free instance does not spin down.

A free Render web service spins down after inactivity. The first request after idle waits through a cold start. Tasks stay in Neon either way.
