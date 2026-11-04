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
- htmx:beforeSwap fires on the swap target, not on the element that made the request, so for a #thread response event.target is the old #thread. Decide whether to drop a response from the conversation in event.detail.requestConfig.path, never from event.target.
- ⌘+Enter or Ctrl+Enter sends the reply through one keydown listener on document that calls requestSubmit(). Enter alone is a new line. Don't bind handlers to #composer-text; swaps replace it.
- #bubbles data-last is the newest message's id. The chat scrolls to the bottom only when data-last changes or another conversation opens (inboxPlaceBubbles); any other swap restores scrollTop. _thread_context returns messages as a list so the template reads the newest without another query.
- #inbox data-open is the conversation the user last clicked. #thread data-conversation is the one on screen. Drop a #chat-list response whose data-requested is not data-open, and a row's #thread response that is not for data-open.
- Inbox htmx requests return partials: _list.html for the list poll, which also carries the open conversation body out of band, and _thread.html for a thread. They set request.inbox_partial so the nav context processor runs no queries. Do not render inbox.html for a poll.
- The list poll sends X-Inbox-Version, which is #chat-list data-version, and the view answers 204 when _list_version is unchanged. Add anything new the list or open conversation shows to _list_version, or polls will not pick it up. The poll pauses while the tab is hidden.
- A #chat-list swap replaces the scrolling aside. Restore its scrollTop after the swap. Only a click moves a row to the top.
- On phones (max-width 768px) the inbox shows one pane at a time, chosen by #inbox data-view (list or thread). A row click and inboxShowList set it in the browser, so no request may replace #inbox. Check layout changes at 390px as well as on desktop.
- Every query and Redis call crosses the network to Neon or Upstash. Keep the Postgres pool and the shared Redis client, and count queries when adding to an inbox request.
- When a UI fix does not hold, find where the wrong value first appears before adding another guard. Log the value read and the value expected side by side.
- Remove debug logging, such as #region agent log blocks and .cursor/debug-*.log writers, before finishing.

## Mercado Libre

- An organization connects at most one Mercado Libre seller (OneToOne on MercadoLibreAccount and SellerProfile). One seller may be connected to several organizations.
- Each organization keeps its own MercadoLibreAccount row. Refresh tokens work only once, so a refresh or a reconnect writes the new tokens to every row with that meli_user_id (fresh_access_token, share_tokens).
- A webhook for a seller fans out: one MercadoLibreNotification row per connected organization, and each organization gets its own ItemQuestion and Conversation rows. Look up questions and packs by organization plus id, never by id alone.
- Check the resource format in stored MercadoLibreNotification rows before writing a parser. Questions arrive as /questions/<id>. Post-sale messages arrive as a bare message id with no slash. GET /messages/<id> can return the message inside a messages list, like a thread; read it with postsale._message_in, and take the pack id, or the order id when the sale has no pack.
- The background worker takes jobs with one BRPOP over every queue (run_next_job), replies first. Redis reads a BRPOP timeout of 0 as wait forever, so never pass 0. A new queue goes in WORKER_QUEUES and the handler map.
- A laptop and Render share Neon and Upstash. Every Redis key goes through jobs._key, under REDIS_PREFIX: Render uses vimple, a laptop uses vimple-dev. With the same prefix, either worker takes the other's jobs.
- The laptop's SECRET_KEY differs from Render's, so Mercado Libre tokens and the parser key saved on Render can't be decrypted locally (InvalidToken), and the other way round. Don't connect Mercado Libre or save the parser key from a laptop against the shared database.
- Background jobs log every failure with describe_failure (error class and Mercado Libre's reason, never a token). Never pass silently on an exception.
- The webhook stores the row and calls dispatch_notification. It makes no Mercado Libre calls, because Mercado Libre expects an answer in about 500 ms. The worker loads the notification.
- Fetch post-sale messages with tag=post_sale and mark_as_read=false. Link the post-sale header to /ventas/<id>/detalle, not to the messages page, because opening the messages page marks the buyer's message read.
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

## Tests

- With DATABASE_URL set, manage.py test runs on the remote Neon database and is slow. Run DATABASE_URL= uv run python manage.py test --noinput to use in-memory SQLite.
- Always pass --noinput. Without it, a leftover test database makes Django wait for an answer that never comes.
