# What we fixed in the inbox, and how

This guide covers two pieces of work on the inbox:

1. **A bug.** Clicking a conversation highlighted the wrong row, and the list sometimes stopped updating.
2. **Speed and UX.** Every click and poll took 4–6 seconds. We also moved the active conversation to the top, changed the colors and added a loading modal.

Each part covers what went wrong, how we found it, how we fixed it and what you can reuse. Links to the official docs are at the end of each section.

---

## Part 1: The "wrong conversation highlighted" bug

### What you saw

When you clicked conversation B, the right pane showed B but the list highlighted A, the one you just left. Sometimes the list stopped updating altogether.

### Step 1: Read the logs before changing code

Grok had already added debug logging that wrote to `.cursor/debug-cd02a5.log`. The answer was already in it:

```
"thread swapped"  dataConversation: f001…   location: ?conversation=52a8…
```

So the URL said we were on `52a8…`, but the code that runs after the swap read `f001…`, the previous conversation. Every `thread swapped` line had the same mismatch. Something was reading a stale value.

> **Lesson:** when a fix "doesn't hold", don't add another guard on top. Find where the wrong value first appears. Log *the value you read* next to *the value you expected*. The first place they differ is the bug.

Grok kept adding guards (`data-shown`, `allow`, "highlight kept"), and each one was built on the wrong value. That's why it went back and forth.

### Step 2: Understand the cause, an htmx outerHTML swap

The row used `hx-swap="outerHTML"` to replace `<section id="thread">`. With `outerHTML`, htmx removes the old element and puts a new one in its place.

The handler looked like this:

```js
document.body.addEventListener("htmx:afterSwap", function (event) {
  var target = event.detail.target;                    // the OLD #thread
  var open = target.getAttribute("data-conversation"); // the previous id
  ...
});
```

After an `outerHTML` swap, `event.detail.target` is still the **old element**, which is no longer on the page. Its `data-conversation` is the conversation you just left, and the code saved it as "open".

The fix was to read the element that is on the page now:

```js
var thread = document.getElementById("thread");      // the NEW #thread
var shown = thread.getAttribute("data-conversation");
```

You can check this yourself in the browser console: keep a reference to the old element and check `oldElement.isConnected`. After the swap it's `false`.

### Step 3: Why the repair handler never ran

There was also an `htmx:afterOnLoad` handler meant to fix the highlight, and it never ran: there were zero `row loaded` entries in the log. The thread response also sent the clicked row back as an "out-of-band" swap, `hx-swap-oob`, which **replaced the row you clicked**. The event then fired on that removed row. Events bubble up through parent elements, and a removed element has none, so the event never reached the listener on `document.body`.

> **Lesson:** events from an element that a swap removed never reach `document.body`.

### Resources
- htmx `hx-swap` (innerHTML vs outerHTML): https://htmx.org/attributes/hx-swap/
- htmx out-of-band swaps: https://htmx.org/attributes/hx-swap-oob/
- htmx events reference (`htmx:beforeSwap`, `htmx:afterSwap`, …): https://htmx.org/events/
- MDN `Node.isConnected`: https://developer.mozilla.org/en-US/docs/Web/API/Node/isConnected
- MDN event bubbling (`Event.bubbles`): https://developer.mozilla.org/en-US/docs/Web/API/Event/bubbles
- Julia Evans on debugging: https://jvns.ca/blog/2019/06/23/a-few-debugging-resources/

---

## Part 2: Why the inbox was slow

### Step 1: Measure before guessing

Instead of guessing, we wrote a small script that called the inbox view directly against the real Neon database and Upstash Redis. It made no writes, and it counted queries and timed each step:

| What | Time |
|---|---|
| Opening a new Postgres connection | **1.3 s** |
| One query (network round trip) | ~170 ms |
| One Redis call (new connection every time) | **~1.1 s** |
| One inbox request (11–13 queries + 2 Redis connections) | **4.0–4.5 s** |

Django's `CaptureQueriesContext` records every SQL query that runs inside a `with` block. It's the quickest way to see what a view really does.

> **Lesson:** a database query isn't "free" just because it's fast on the server. When the database is in another region, **every query pays the full network round trip**. 13 queries × 170 ms is more than 2 seconds of waiting.

Some rough numbers to remember: inside one datacenter a round trip is ~0.5 ms, across a continent ~50–70 ms, and from Argentina to Ohio ~170 ms. See the "latency numbers every programmer should know" link below.

### Step 2: The causes, and the fix for each

#### 1. A new Redis connection for every call
`meli/jobs.py:_client()` created a new `redis.Redis(...)` on every call. Each one opened a fresh TLS connection to Upstash, which took ~1 s.

**Fix:** create the client once and reuse it. A redis-py client has a **connection pool** built in, so the first call connects and later calls reuse that connection.

```python
_clients = {}
_client_lock = threading.Lock()

def _client():
    url = settings.REDIS_URL
    with _client_lock:              # several threads can call this at once
        client = _clients.get(url)
        if client is None:
            client = redis.Redis.from_url(url, ...)
            _clients[url] = client
    return client
```

Result: the second call dropped from 1064 ms to 155 ms.

#### 2. A new Postgres connection for every request
`CONN_MAX_AGE: 0` tells Django to close the database connection at the end of every request, so the next request reconnects (1.3 s).

**Fix:** Django 5.1+ has a built-in **connection pool** (from `psycopg_pool`):

```python
options["pool"] = {"min_size": 1, "max_size": 8, "timeout": 10}
```

When a request ends, Django gives the connection back to the pool instead of closing it. Why a pool and not `CONN_MAX_AGE`? `CONN_MAX_AGE` keeps one connection per **thread**, and the dev server starts a new thread for every request, so it doesn't help there. A pool is shared by all threads.

#### 3. Production could handle only one request at a time
`gunicorn --workers 1` with the default "sync" worker handles **one request at a time**. Two pollers every 3 s, each taking seconds, kept that one worker busy, and your click had to wait in line.

**Fix:** `--threads 4`. Still one process, but 4 requests can run at once. Most of a request is spent waiting on the network, so threads help a lot here.

#### 4. Small responses did work for the whole page
- A thread click returned only `_thread.html`, but Django still ran the **context processor** `nav()`, which made ~7 queries for the sidebar. That response doesn't even include the sidebar.
- The pollers rendered the **entire** `inbox.html` page, and `hx-select` then discarded everything except one fragment.

**Fix:**
- Polls and clicks return small partial templates (`_list.html`, `_thread.html`).
- The view sets `request.inbox_partial = True`, and `nav()` returns `{}` when it sees it.
- The two pollers (list + open conversation) became **one**. The list response also carries the open conversation's messages as an out-of-band fragment (`hx-swap-oob`). That halves the number of requests.

Result: thread click and list poll went from **~4 s / 11–13 queries** to **~0.7 s / 3 queries** locally.

#### 5. Region (not a code change)
The app runs on Render's default region (Oregon) and the database is in Ohio, so every query crosses the country. Putting the app in the same region as the database brings each round trip down to about 1 ms. Code can't fix that.

> **Lesson:** to speed up a slow page, (1) make fewer network calls, (2) make each call cheaper by reusing connections, (3) don't block other requests while you wait, and (4) move the services closer together.

### Resources
- Django database access optimization: https://docs.djangoproject.com/en/5.2/topics/db/optimization/
- Django connection pool (5.1+): https://docs.djangoproject.com/en/5.2/ref/databases/#connection-pool
- Django persistent connections / `CONN_MAX_AGE`: https://docs.djangoproject.com/en/5.2/ref/databases/#persistent-connections
- psycopg connection pools: https://www.psycopg.org/psycopg3/docs/advanced/pool.html
- redis-py connections and pools: https://redis.readthedocs.io/en/stable/connections.html
- Django context processors: https://docs.djangoproject.com/en/5.2/ref/templates/api/#writing-your-own-context-processors
- gunicorn `threads` setting: https://docs.gunicorn.org/en/stable/settings.html#threads
- gunicorn worker types (sync vs threads): https://docs.gunicorn.org/en/stable/design.html
- Render regions: https://render.com/docs/regions
- Neon regions: https://neon.com/docs/introduction/regions
- Latency numbers every programmer should know: https://gist.github.com/jboner/2841832

---

## Part 3: The UX changes

### Active conversation on top (server)
In the view, the selected conversation is moved to the front and the rest keep their order:

```python
def _active_first(conversations, selected_id):
    rows = list(conversations)
    selected = next((row for row in rows if str(row.pk) == selected_id), None)
    if selected is None:
        return rows, rows[0] if rows else None
    return [selected] + [row for row in rows if row.pk != selected.pk], selected
```

The list is already loaded, so picking the selected row in Python saves one query.

### Instant feedback on click (client)
When you click, the browser updates right away, before the server answers:

```js
function inboxSelectRow(row) {
  inbox.setAttribute("data-open", id);   // remember what the user wants
  inboxMarkActive(id);                   // list.prepend(row) + .active
  if (thread shows something else) inboxLoading(true);
}
```

This is called an **optimistic UI update**: show the result you expect right away, and let the server response confirm it.

### Loading modal without a flash
The modal appears only if loading takes longer than **150 ms**:

```js
inboxLoadingTimer = setTimeout(function () { modal.hidden = false; }, 150);
```

If the response arrives first, the timer is cancelled and you never see it. A loading box that flashes for 50 ms makes a page feel slower, not faster. The modal is also hidden on errors (`htmx:sendError`, failed `htmx:afterRequest`), so it can't stay stuck.

### Out-of-date responses: the "last click wins" rule
With polling and fast clicks, an old response can arrive *after* the user has moved on. Each response says what it was requested for, and the client drops responses that no longer match:

- The list partial carries `data-requested="<conversation id>"`. If that isn't what `#inbox[data-open]` holds now, the list response is dropped (`event.detail.shouldSwap = false` in `htmx:beforeSwap`).
- A thread response that came from a row other than the last one clicked is dropped as well.

The first version of the guard compared the response's *active row* instead. That would have **frozen the list forever** whenever the server fell back to a different conversation, for example one hidden by the "Last 7 days" filter. Comparing *what was asked for* is more reliable than comparing *what came back*.

### Colors
- Hover: `#f3e8ff` (light purple)
- Active: `#d8b4fe` + a `#7c3aed` left border (darker, and the extra border makes it obvious)
- `a.active:hover` keeps the active color, so hovering the active row doesn't make it look unselected.

### Resources
- htmx `hx-select`: https://htmx.org/attributes/hx-select/
- htmx `htmx:beforeSwap` / `shouldSwap`: https://htmx.org/events/#htmx:beforeSwap
- MDN `Element.prepend()`: https://developer.mozilla.org/en-US/docs/Web/API/Element/prepend
- MDN `setTimeout` / `clearTimeout`: https://developer.mozilla.org/en-US/docs/Web/API/Window/setTimeout
- MDN `hidden` attribute: https://developer.mozilla.org/en-US/docs/Web/HTML/Global_attributes/hidden
- MDN CSS `:hover` and specificity: https://developer.mozilla.org/en-US/docs/Web/CSS/Specificity

---

## Part 4: Tests that keep it fixed

- **`assertNumQueries(7)`** on a thread click. If someone brings back the nav queries, the test fails. This is the cheapest way to stop slow code from coming back.
- **Order:** the selected conversation appears before the others in the HTML.
- **Partials:** the list poll response starts with `<aside id="chat-list"` and contains no `<html>` or sidebar.
- **Organization isolation (from `AGENTS.md`):** asking for another organization's conversation id never returns it.
- **Redis client reuse:** `_client() is _client()`.

Running the tests:

```bash
DATABASE_URL= uv run python manage.py test --noinput
```

- `DATABASE_URL=` (empty) makes the settings use an in-memory SQLite database. That takes about 3 seconds, compared with minutes over the network to Neon.
- `--noinput` stops Django from asking "delete the old test database?". Without it, a background run waits for an answer that never comes, which is why the first test run seemed to hang.

### Resources
- Django `assertNumQueries`: https://docs.djangoproject.com/en/5.2/topics/testing/tools/#django.test.TransactionTestCase.assertNumQueries
- Django `test --noinput`: https://docs.djangoproject.com/en/5.2/ref/django-admin/#cmdoption-test-noinput
- Django testing overview: https://docs.djangoproject.com/en/5.2/topics/testing/overview/

---

## Part 5: Follow-up fix, the list kept jumping to the top

### What you saw
You scrolled down the conversation list, and every few seconds it jumped back to the top.

### Why
The list poll used `hx-swap="outerHTML"`, so every 3 s the scrolling `<aside>` was **replaced by a new element**. A new element starts at `scrollTop = 0`. It's the same thing as the bug in Part 1: an outerHTML swap gives you a new element, and anything stored on the old one is gone, scroll position included.

### Fix 1: Don't swap when nothing changed (HTTP 204)
- The server computes a short **fingerprint** (`_list_version`, a sha1) of everything the poll shows: the rows' ids, `updated_at` and previews, the open conversation and its messages, and the reply flags.
- The page stores it in `data-version` and sends it back on every poll as the `X-Inbox-Version` header.
- If nothing changed, the server answers **`204 No Content`** with an empty body, and htmx doesn't swap anything.

So most polls cost a tiny response and touch nothing on screen. This is the same idea as HTTP `ETag` / `If-None-Match` (which returns `304 Not Modified`).

### Fix 2: Keep the scroll position when it does change
When there is news, the list is still swapped. Before the swap we save `scrollTop`, and after it we set it on the new element. Only a **click** moves a row to the top. A poll keeps whatever order the server sent.

### Fix 3: Don't poll a tab nobody is looking at
```html
hx-trigger="every 3s [document.visibilityState === 'visible'],
            visibilitychange[document.visibilityState === 'visible'] from:document"
```
The `[...]` part is an htmx **trigger filter**: the poll only fires while the tab is visible. When you come back to the tab, the `visibilitychange` event triggers one refresh right away.

> **Lesson:** polling is fine for small apps, as long as each poll is cheap when nothing changed and stops when nobody is looking. The next step up is server push (Server-Sent Events or WebSockets), which costs more to run and isn't needed yet.

### Resources
- MDN HTTP 204 No Content: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/204
- MDN ETag (the standard version of this idea): https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/ETag
- htmx `hx-trigger` (polling, filters, `from:`): https://htmx.org/attributes/hx-trigger/
- htmx response handling (why 204 doesn't swap): https://htmx.org/docs/#response-handling
- MDN Page Visibility API: https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API
- MDN `Element.scrollTop`: https://developer.mozilla.org/en-US/docs/Web/API/Element/scrollTop
- htmx Server-Sent Events extension (for later): https://htmx.org/extensions/sse/

---

## Takeaways

1. **Read the evidence first.** The log already showed the bug; nobody had compared the two values side by side.
2. **Fix the cause, not the symptom.** Guards built on a wrong value only move the problem around.
3. **After a DOM swap, read elements from the page.** `document.getElementById(...)`, not a reference you held before the swap.
4. **Measure before optimizing.** The measurements pointed at Redis and connection setup, which nobody had suspected.
5. **Count network round trips.** Fewer requests, fewer queries, reused connections, services in the same region.
6. **Update the screen right away, and drop responses that arrive too late.** Users see the result instantly, and slow responses can't undo it.
7. **Write tests that fail if the problem comes back.** Query counts, order, and organization isolation.
8. **Cheap polls:** answer 204 when nothing changed, and pause when the tab is hidden.

## Try it yourself

1. In the browser DevTools Network tab, watch the inbox for 10 seconds. Count the requests and look at the response size of a poll.
2. In a test, wrap a view call in `CaptureQueriesContext` and print each query. Can you find one that could be removed?
3. Temporarily change the modal delay from 150 ms to 0 and click around. Notice the flash? That's why the delay is there.
