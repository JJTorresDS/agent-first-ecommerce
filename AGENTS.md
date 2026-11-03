# Agents

Read this before changing models, queries, pages, or APIs.

Organization is the tenant. Its UUID does not change. The name can.

- App models other than Organization carry an organization foreign key, through TenantModel or an explicit field such as ItemQuestion.organization.
- Pages take the organization from membership_for(request.user). A user with no membership goes to organization setup and sees no other organization's data.
- API list, get, create, and update calls take the organization id and filter on it. A row in another organization is not returned and is not moved.
- Counts, badges, exports, and webhook rows use that same organization. A query with no organization filter is wrong.
- A new read or write includes a test where another organization's row stays invisible.
- Leave the tasks app label, its database table, /api/tasks, and the AppAccess value tasks as they are.

## Inbox and htmx

- After an outerHTML swap, event.detail.target in htmx:afterSwap is the element that was removed. Read the new one with document.getElementById. Events on an element that a swap replaced, such as a row replaced by hx-swap-oob, never reach document.body.
- #inbox data-open is the conversation the user last clicked. #thread data-conversation is the one on screen. Drop a #chat-list response whose data-requested is not data-open, and a row's #thread response that is not for data-open.
- Inbox htmx requests return partials: _list.html for the list poll, which also carries the open conversation body out of band, and _thread.html for a thread. They set request.inbox_partial so the nav context processor runs no queries. Do not render inbox.html for a poll.
- The list poll sends X-Inbox-Version, which is #chat-list data-version, and the view answers 204 when _list_version is unchanged. Add anything new the list or open conversation shows to _list_version, or polls will not pick it up. The poll pauses while the tab is hidden.
- A #chat-list swap replaces the scrolling aside. Restore its scrollTop after the swap. Only a click moves a row to the top.
- Every query and Redis call crosses the network to Neon or Upstash. Keep the Postgres pool and the shared Redis client, and count queries when adding to an inbox request.
- When a UI fix does not hold, find where the wrong value first appears before adding another guard. Log the value read and the value expected side by side.
- Remove debug logging, such as #region agent log blocks and .cursor/debug-*.log writers, before finishing.

## Tests

- With DATABASE_URL set, manage.py test runs on the remote Neon database and is slow. Run DATABASE_URL= uv run python manage.py test --noinput to use in-memory SQLite.
- Always pass --noinput. Without it, a leftover test database makes Django wait for an answer that never comes.
