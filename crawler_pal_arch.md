**Generated with GLM-5.3-flash**

# Architecture Overview: CrawlerPal News Demo (L402 Paywall)

This repo is a compact two-part demo showing how to put API/data access behind an **L402-style paywall** (the "HTTP 402" protocol for machine payments) using **PayPal** as the payment rail. It's entirely Python — no frontend framework, no real database.

```
┌─────────────────┐        ┌──────────────────────────┐        ┌───────────────────┐
│  client/main.py │ ─────► │  server/main.py (FastAPI)│ ─────► │ PayPal Sandbox API│
│  (requests CLI) │ ◄───── │  uvicorn on :8000        │ ◄───── │  v2/checkout/orders│
└─────────────────┘        └──────┬───────────────────┘        └───────────────────┘
                                  │
                                  ▼
                          payments_db.json  (persistent token store)
                          Faker-generated news (in-memory)
```

## 1. Server (`server/main.py`)

A single-file FastAPI app run with `uvicorn` on port 8000. It has three core pieces:

### a) Content generation

- On startup, it generates mock news with **Faker** (4 items × 6 categories: politics, international, economy, technology, sports, entertainment) and keeps it in memory as `news_data`.

### b) The gatekeeper: `GET /`

This endpoint does **client-type sniffing and auth in one place**:

1. **Browser vs. bot detection** — checks the `User-Agent` header against a list of common browser strings (`is_browser()`).
   - **Browser** → returns a hand-built HTML news page (all content free).
   - **Bot/programmatic** → proceeds to auth check.
2. **Token validation** — extracts a `Bearer` token via the `get_authorization_header` dependency and checks whether the token exists as a key in `payments_data` (the in-memory dict loaded from `payments_db.json`).
   - **Valid token** → looks up the payment record's `offer_id`:
     - `all_categories` → full JSON `{"news": [...]}`
     - `one_category` → same, filtered by the recorded `category`
   - **No/invalid token** → returns **HTTP 402 Payment Required** with an L402 challenge: a fresh `payment_context_token` (UUID), a `payment_request_url` pointing to `/l402/payment-request`, and a static list of two offers:
     - `one_category` ($0.01)
     - `all_categories` — labeled "monthly subscription" ($0.05)

### c) Payment endpoint: `POST /l402/payment-request`

This is where the money moves:

1. Maps `offer_id` → dollar amount.
2. Builds a PayPal **Orders API v2** payload (`intent: CAPTURE`) using `httpx.AsyncClient`, authorized with a `PAYPAL_ACCESS_TOKEN` loaded from `.env` via `python-dotenv`, plus a unique `PayPal-Request-Id` header for idempotency. (It includes a hardcoded sandbox `vault_id`/customer ID — demo shortcut.)
3. Calls the PayPal sandbox; success is determined by status `COMPLETED`.
4. On success, it **stores the payment record keyed by the `payment_context_token`** — offer, timestamp, PayPal order ID, category — in `payments_data` and persists it to `payments_db.json` (a JSON-file "database", loaded on startup via the `startup` event).
5. Returns `{"message": ..., "bearer_token": <payment_context_token>}` — the payment context token *becomes* the access credential.

## 2. Client (`client/main.py`)

A `requests`-based CLI with three mutually exclusive modes that together implement the **full L402 client dance**:

1. **Default (no args):** plain `GET` to the server — mimics a naive bot, receives the 402 challenge and prints the offers.
2. **`--pay [--category X]`:** the automated agent flow:
   - `GET /` → capture the 402 body, extract `payment_request_url` + `payment_context_token`
   - `POST` the payment URL with `{payment_context_token, offer_id, category?}`
   - On success, prints the returned **Bearer token** for later use
3. **`--with-auth TOKEN`:** `GET /` with an `Authorization: Bearer <token>` header → receives the JSON news data the token is entitled to.

## 3. End-to-end request lifecycle

```
Agent                     Server                        PayPal
  │ GET / (no auth)          │                             │
  │─────────────────────────►│  bot + no token             │
  │◄─── 402 + offers + ctx───│                             │
  │ POST /l402/payment-request                             │
  │─────────────────────────►│  create & capture order ───►│
  │                          │◄─── COMPLETED ──────────────│
  │                          │  payments_db[token] = offer │
  │◄─── bearer_token ────────│                             │
  │ GET / (Bearer token)     │                             │
  │─────────────────────────►│  validate + scope check     │
  │◄─── 200 JSON news ───────│  (filtered by category)     │
```

## 4. Key design points & limitations

- **Token = payment context UUID**: entitlements are looked up directly by the Bearer token; no signature, expiry, or scope validation beyond the stored offer/category.
- **Two-tier access control**: the offer (`all_categories` vs `one_category`) is enforced server-side at read time by filtering the news list.
- **Humans free, bots pay**: the paywall only applies to non-browser User-Agents — browsers get the full HTML site.
- **Persistence is a JSON file** with an in-memory mirror — fine for a demo, not for concurrency.
- **Trusts the client's payment claim implicitly** — the server itself drives the PayPal charge with hardcoded sandbox credentials rather than integrating a real client-side PayPal checkout, so payment "authorization" is really just the server confirming its own sandbox call succeeded.

In short: it's a minimal reference implementation of the L402 pattern — *402 challenge → client-initiated payment → token issuance → token-authenticated data access* — with PayPal swapped in for the usual Lightning micropayments.
