# Agents

Read this before changing models, queries, pages, or APIs.

Organization is the tenant. Its UUID does not change. The name can.

- App models other than Organization carry an organization foreign key, through TenantModel or an explicit field such as ItemQuestion.organization.
- Pages take the organization from membership_for(request.user). The user's memberships are read once per request (memberships_for, cached on the user; any Membership, Profile or Organization write clears it), so the middleware, the view and the nav share one query. A user with no membership goes to organization setup and sees no other organization's data.
- API list, get, create, and update calls take the organization id and filter on it. A row in another organization is not returned and is not moved.
- Counts, badges, exports, and webhook rows use that same organization. A query with no organization filter is wrong.
- A new read or write includes a test where another organization's row stays invisible.
- Leave the tasks app label, its database table, /api/tasks, and the AppAccess value tasks as they are.

## Look

- Colors are the :root variables in templates/base.html, taken from the logo: charcoal (--ink) and grays, with neon green (--neon) only for accents: the active menu item, focus rings, the one main action per page (.accent), SKU chips (.sku) and active status pills. Don't hard-code new colors.
- Check new pages at 390px as well as on desktop; wide tables go in .table-scroll.
- The profile menu sits inside the dark sidebar but sets its own colors (.profile-panel); nothing in it may inherit from .sidebar.
- Each organization row in the menu has two buttons in one switch form: the name (go=home, the organization page) and the message icon with its count (go=inbox). organization_switch only follows those two names, never a URL from the request.
- Chats waiting per organization and channel come from one grouped query over the user's own memberships (inbox.unread.waiting_counts). The nav reads the badges and the bell alerts from it; don't count conversations again. The avatar badge counts only other organizations. /profile/unread/ is polled every 20 s, also while the tab is hidden (the chime matters most when you're in another tab), at once after an inbox list poll brings changes, and answers 204 when X-Unread-Version matches (with Redis, from the change marks of the user's organizations, without reading chats). latest is epoch milliseconds, compared as numbers; the poll sends since=latest and gets new, the count of buyer messages after it (inbox.unread.latest_incoming), and plays one Web Audio chime per message (up to three), even when the waiting total is unchanged or the messages are from the same buyer (on by default, switch in the menu). The audio context is made on page load, not on the first click, and a Worker drives the 20 s timer so hidden tabs aren't throttled. Tabs share the last chimed message in localStorage, and only a tab that actually sounded claims it, so a tab the browser keeps silent never mutes another.

## Inbox and htmx

- After an outerHTML swap, event.detail.target in htmx:afterSwap is the element that was removed. Read the new one with document.getElementById. Events on an element that a swap replaced, such as a row replaced by hx-swap-oob, never reach document.body.
- htmx:beforeSwap fires on the swap target, not on the element that made the request, so for a #thread response event.target is the old #thread. Decide whether to drop a response from the conversation in event.detail.requestConfig.path, never from event.target.
- ⌘+Enter or Ctrl+Enter sends the reply through one keydown listener on document that calls requestSubmit(). Enter alone is a new line. Don't bind handlers to #composer-text; swaps replace it.
- A sent reply shows at once: htmx:beforeRequest on #composer adds a .bubble.outgoing with "Sending…" and clears the box (htmx has already read the form by then). The #thread answer replaces it; a list poll that lands first re-adds it; a send or response error removes it and puts the text back. Don't wait for the server to show what the user sent.
- #bubbles data-last is the newest message's id. The chat scrolls to the bottom only when data-last changes or another conversation opens (inboxPlaceBubbles); any other swap restores scrollTop. _thread_context returns messages as a list so the template reads the newest without another query.
- #inbox data-open is the conversation the user last clicked. #thread data-conversation is the one on screen. Drop a #chat-list response whose data-requested is not data-open, and a row's #thread response that is not for data-open.
- Inbox htmx requests return partials: _list.html for the list poll, which also carries the open conversation body out of band, and _thread.html for a thread. They set request.inbox_partial so the nav context processor runs no queries. Do not render inbox.html for a poll.
- The list poll sends X-Inbox-Version, which is #chat-list data-version. With Redis, the version is _poll_token: the organization's change mark, the open chat's reply status and error, the query and the minute, read in one Redis round trip, so an unchanged poll answers 204 before any Postgres read. Every write to a Conversation or Message calls inbox.services.inbox_changed (after commit), or polls won't see it for up to a minute. Without Redis the version is _list_version over the rows; add anything new the list or open conversation shows there. The poll pauses while the tab is hidden.
- Webhooks keep chats current. A full inbox load queues the Mercado Libre refresh; a list poll does so only when the 10-minute safety window (INBOX_POLL_REFRESH) has run out.
- A #chat-list swap replaces the scrolling aside. Restore its scrollTop after the swap. Only a click moves a row to the top.
- On phones (max-width 768px) the inbox shows one pane at a time, chosen by #inbox data-view (list or thread). A row click and inboxShowList set it in the browser, so no request may replace #inbox. Check layout changes at 390px as well as on desktop.
- Writing a chat (inbox.services.upsert_thread) is bulk: one read of its messages, one bulk_create, one bulk_update (set updated_at yourself; bulk_update skips auto_now), one delete. Never one query per message: a 35-message chat once took 154 queries, 6-10 s on Render. A sent reply is one row (meli.postsale.remember_reply), about 6 queries.
- Every query and Redis call crosses the network to Neon or Upstash. Keep the Postgres pool and the shared Redis client, and count queries when adding to an inbox request.
- When a UI fix does not hold, find where the wrong value first appears before adding another guard. Log the value read and the value expected side by side.
- Remove debug logging, such as #region agent log blocks and .cursor/debug-*.log writers, before finishing.

## Mercado Libre

- An organization connects at most one Mercado Libre seller (OneToOne on MercadoLibreAccount and SellerProfile). One seller may be connected to several organizations.
- Each organization keeps its own MercadoLibreAccount row. Refresh tokens work only once, so a refresh or a reconnect writes the new tokens to every row with that meli_user_id (fresh_access_token, share_tokens).
- A webhook for a seller fans out: one MercadoLibreNotification row per connected organization, and each organization gets its own ItemQuestion and Conversation rows. Look up questions and packs by organization plus id, never by id alone.
- Check the resource format in stored MercadoLibreNotification rows before writing a parser. Questions arrive as /questions/<id>. Post-sale messages arrive as a bare message id with no slash. GET /messages/<id> can return the message inside a messages list, like a thread; read it with postsale._message_in, and take the pack id, or the order id when the sale has no pack.
- Each server process runs two worker threads: one takes only replies (REPLY_LANE), the other everything else (WORKER_QUEUES) with one BRPOP over its queues (run_next_job). A reply never waits behind a sync or refresh; each logs 'sent in … after waiting …'. Never put a slow job on the reply lane. Redis reads a BRPOP timeout of 0 as wait forever, so never pass 0. A new queue goes in WORKER_QUEUES and the handler map.
- A laptop and Render share Neon and Upstash. Every Redis key goes through jobs._key, under REDIS_PREFIX: Render uses vimple, a laptop uses vimple-dev. With the same prefix, either worker takes the other's jobs.
- The laptop's SECRET_KEY differs from Render's, so Mercado Libre tokens and the parser key saved on Render can't be decrypted locally (InvalidToken), and the other way round. Don't connect Mercado Libre or save the parser key from a laptop against the shared database.
- Background jobs log every failure with describe_failure (error class and Mercado Libre's reason, never a token). Never pass silently on an exception.
- The webhook stores the row and calls dispatch_notification. It makes no Mercado Libre calls, because Mercado Libre expects an answer in about 500 ms. The worker loads the notification.
- MeliListing is one row per (item, variation), linked to catalog.MasterProduct by SKU: the SELLER_SKU attribute, else seller_custom_field, the variation's own first (meli/listings.py sku_of). Items are fetched with include_attributes=all, or variation SKUs are missing. One product can have many listings.
- The full listing sync scans every status (active, paused, closed, under_review, inactive) and deletes rows for items it didn't see only after every scan succeeded. items webhooks refresh one item; a 404 removes its rows. MeliListingSync holds the last run's state and counts.
- New listings are ListingDrafts (meli/drafts.py, page /catalog/meli/new/). draft → checking → ready or needs_fix → queued → publishing → published or failed. A draft is checked with POST /items/validate before it can be posted, so Mercado Libre's own reason shows on the draft. Checking and posting run in the worker (DRAFTS_QUEUE, dispatch_drafts); pages never call Mercado Libre. A posted draft is claimed (queued → publishing) before POST /items, so a repeated click or job creates one item.
- A draft's quantity defaults to 1 and is never above the Depósito stock, checked when saved and again just before posting. No draft without a first inventory count. SELLER_SKU is the Vimple SKU, so the sync links the new listing; after posting, refresh_listing stores it.
- build_item sends what Mercado Libre's sell flow sends (ids read from synced payloads): INVOICE 6891888 No factura / 6891885 Factura A, WARRANTY_TYPE 2230280 seller / 2230279 factory, WARRANTY_TIME "N días", ITEM_CONDITION 2230284, 155_98_RESOLUTION_FILE_NUMBER -1 only when the category has it, gold_special without installments / gold_pro with, shipping me2 and local_pick_up always false. Flex is removed per item after posting when the settings say no.
- A new catalog product takes unlinked listings with its SKU through a post_save receiver in meli/apps.py, so the catalog app never imports meli.
- Fetch post-sale messages with tag=post_sale and mark_as_read=false. Link the post-sale header to /ventas/<id>/detalle, not to the messages page, because opening the messages page marks the buyer's message read.
- A presale answer is one Mercado Libre call (post_answer); the question is re-read in the background.
- A post-sale reply is one Mercado Libre call when Conversation.counterpart_id (the buyer, set when the pack is projected) is known. send_post_sale stores the reply from Mercado Libre's answer under its message id, then re-reads the pack in the background (dispatch_notification). load_pack fetches the order only while the chat has no title or link yet.
- Post-sale chats stay open after a reply (register_sender stays_open). The app does not decide whether a message is allowed: Mercado Libre does, and its refusal is shown. Presale questions hide the composer once answered, because Mercado Libre takes one answer per question.
- _api_request raises MercadoLibreError with Mercado Libre's own reason. Senders turn it into ReplyFailed(message); run_reply_job keeps the reason and the unsent text under reply_error_key for the thread to show.
- A reply is sent in the background. Its bubble shows "Sending…" while #thread polls with poll=sending, which keeps the composer (hx-preserve) so a draft survives. Nothing blocks the page.

## Cashflow receipt parser

- CashflowParserSettings holds one organization's receipt folders. build_payload must keep the body the n8n trigger sent: organization_id plus files: [{organization_id, organization_name, receipts_folder, cost, profit}]. receipt-parserV2 reads that shape.
- Each organization's owner or admin enters the parser key in Cashflow settings. It is stored encrypted (meli.tokens encrypt_secret) in CashflowParserSettings.api_key and sent as X-API-Key. Never render it, pre-fill it, log it, or put it in last_run_error.
- Drive tokens belong to one user, not to the organization. The drive-token view returns only request.user's own token. Sign-in asks only for profile and email; the drive.file scope is added by Connect Google Drive.

## Catalog and purchasing

- SKU = CATEGORY-BRAND-MODEL-VARIANT (PERF-ONLY-885-30ML), built from the codes on a product's first save and never changed, because channels match by SKU. Codes are A-Z and 0-9; the hyphen only separates them. They are capitalized in clean_fields, before the validators run.
- Category and Brand codes and names are unique per organization; a ProductModel code is unique within its brand. A code, or a product's model and variant, can't change once a SKU uses it: create a new one.
- ProductModel is the variant family and holds shared data (name, description, condition). MasterProduct is one variant: the variant is its only free part. Secondary attributes go in attributes, a flat JSON object checked by catalog.validators.validate_attributes. Media lives outside Vimple: only image_url is stored.
- Channel listings (Mercado Libre, Tiendanube) will be their own models pointing at MasterProduct, not keys in its JSON.
- purchasing has Supplier, Purchase (one invoice: supplier, date, currency, optional tasks.CashflowMovement) and PurchaseItem (product, quantity, unit_cost). Cost history comes from the lines: MasterProduct.costs_by_supplier.
- A purchase, its supplier, its receipt and its lines' products are in one organization. Purchase and PurchaseItem refuse anything else in clean() and save(); lines take the purchase's organization.
- A product with purchases can't be deleted (PROTECT). Archive it with status archived.
- Pages: Catalog (/catalog/) and Purchases (/purchases/) sit under "Products" in the sidebar, each behind its own AppAccess (catalog, purchasing). Anyone granted reads; owners and admins edit (can_edit_products), and a POST from anyone else is 403.
- Catalog pages show costs only to users who can also open Purchasing. Purchase forms offer cashflow receipts only to users who can open Cashflow.
- Forms take the organization from membership_for, never from the request. Use catalog.forms.TenantForm so organization-scoped unique checks are form errors, and limit every select's queryset to the organization.
- Purchase lines name products by SKU (purchasing.forms.read_lines), resolved inside the organization; a line id from another purchase is never moved.
- A + next to a select (category, brand, supplier) opens a dialog outside the page's form. Its view saves with the organization's TenantForm and answers 204 with HX-Trigger select-option-added {select, id, label, dialog}; base.html adds and selects the option. Errors re-render the dialog's form (200).

## Inventory

- Stock is only ever written as an inventory.StockMovement, through inventory/ledger.py. On-hand is the sum of movements per product and location; never store or edit a stock number.
- Locations: the organization's Depósito (own) and Mercado Libre Full (meli_full), created on first use.
- Every automatic movement has a source_key (receipt, sale, cancel), unique per organization, so a webhook or click that repeats changes nothing.
- A purchase adds stock only when received (ledger.receive), into the Depósito, never more than ordered. A received line can't be removed, lowered below what arrived, or change product.
- Mercado Libre sales (meli.MeliSale, one per order line) come off stock only after InventorySettings.started_at (the first count), on a listing linked to a product, as quantity x MeliListing.units_per_sale. A Full listing (logistic_type fulfillment) sells out of Full. A cancelled order puts the units back once. Unlinked sales wait and apply when the listing links.
- orders_v2 webhooks are fetched only for organizations whose inventory has started. Nothing is pushed to Mercado Libre; the stock page compares Vimple with Mercado Libre's numbers.

## Access and impersonation

- Only a superuser can view Vimple as another user, never as a superuser, themselves or an inactive user. A GET shows a confirmation; only a POST switches. The session keeps the superuser id and where to return (Access page or Django admin), written after login(), which empties the session.

## Tests

- With DATABASE_URL set, manage.py test runs on the remote Neon database and is slow. Run DATABASE_URL= uv run python manage.py test --noinput to use in-memory SQLite.
- Always pass --noinput. Without it, a leftover test database makes Django wait for an answer that never comes.
