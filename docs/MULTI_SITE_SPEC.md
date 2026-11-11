# Multi-site gateway + console — implementation spec

## Goal

SARA must support **multiple configured websites** (sites) at once:

1. The gateway proxies every configured site (host-based routing, plus an explicit
   `/_site/{site_id}/...` route for demos on one host, plus `X-SARA-Site` for agents and
   the playground).
2. The admin/publisher console lets the owner **add, remove, pause/resume, and select**
   websites.
3. The **selected/active** website is the one whose deposit, rate limit, access rules,
   policy, and playground are shown and edited at that moment.
4. Demo-mode restrictions are removed:
   - no 10 req/min cap (backend or UI),
   - adding websites is enabled,
   - public config no longer requires `DEMO_ORIGIN_ALLOWLIST` for a new origin.
   SSRF protection for public writes stays (reject private/loopback/reserved IPs); admin
   token holders may point at private/internal origins.
5. Keep the console visually minimal. Reuse the existing site list, `.site-add` CSS, and
   pencil-to-edit controls; do not redesign the page.

## Backend data model

Use the existing `gateway_config_path` file as the registry path so nothing new is needed
in env or Docker. The file becomes:

```json
{
  "active_site_id": "www-example-com",
  "sites": [
    {
      "site_id": "www-example-com",
      "name": "www.example.com",
      "status": "live",
      "created_at": "...",
      "updated_at": "...",
      "config": { "...StoredRuntimeConfig fields...": "..." }
    }
  ]
}
```

`config` is the existing `StoredRuntimeConfig` (reuse it): `origin_url`, `deposit_amount`,
`tier_prices`, `allowed_paths`, `disallowed_paths`, `max_requests_per_session`,
`rate_limit_requests`, `rate_limit_window_seconds`, `policy_version`, `version`,
`updated_at`, `updated_by`.

Backward compatibility: if the file exists but has no `sites` key, treat it as a legacy
flat `StoredRuntimeConfig` and migrate it into one site. If the file is missing, create an
in-memory default site:

- `site_id` = slug of `settings.origin_url` host, else `"default"`
- `name` = host, else `"SARA demo site"`
- `config.origin_url` = `settings.origin_url` or unset/NULL (inherit env)

Do not write the registry merely because the app started; save on the first mutation.

Add `server/services/site_registry.py` with:

- `GatewaySite` pydantic model (`site_id`, `name`, `status: Literal["live","paused"]`,
  `config: StoredRuntimeConfig`, `created_at`, `updated_at`).
- `SiteRegistryData` (`active_site_id: Optional[str]`, `sites: list[GatewaySite]`).
- `SiteRegistryStore(path)` with `load()` (migrating legacy) and `save()` (atomic,
  chmod 600, same style as `RuntimeConfigStore`).
- `slugify_site_id(origin_url)` helper and `host_of(origin_url)` helper.

## Backend runtime

Add a `SiteRuntime` dataclass in `server/dependencies.py`:

```python
@dataclass
class SiteRuntime:
    site: GatewaySite
    content_service: ContentService
    site_config: SiteConfig
    pricing: PricingCatalog
    policy_engine: PolicyEngine
    identity_service: IdentityService
    origin_proxy: Optional[OriginProxy]
```

`ContentService` gets optional `site_id` / `site_name` kwargs and sets those on the built
`SiteConfig`. Add `runtime.pricing_tiers` support: when a site was created with
`pricing_tiers=[]`, use no tiers; when `None`, keep the bundled `data/site_config.json`
tiers. `RuntimeConfigUpdate` gains
`pricing_tiers: Optional[list[PricingTier]] = None`.

`AppContainer`:

- Loads the registry; builds the active site runtime eagerly; other runtimes lazily and
  caches them.
- Keeps the existing attribute names working as **active-site** properties:
  `site_config`, `pricing`, `origin_proxy`, `content_service`, `policy_engine`,
  `identity_service`, `runtime_config`. Existing APIs that are global/console-scoped
  (`commerce`, `demo`, dashboard summary) keep using these.
- Adds:
  - `active_site_id` property,
  - `resolve_site(site_id=None, host=None) -> SiteRuntime` (explicit id wins; else match
    `Host` against each site's origin host; else active),
  - `list_site_summaries() -> SiteListResponse`,
  - `add_site(...) -> RuntimeConfigResponse`,
  - `update_site_meta(site_id, name=None, status=None) -> SiteSummary`,
  - `delete_site(site_id)` (keep at least one site; if the active site is deleted,
    activate the first remaining),
  - `set_active_site(site_id) -> SiteListResponse`,
  - `runtime_config_response(site_id=None)` with `site_id`, `site_name`, `status`,
    `active_site_id`, `host` added,
  - `apply_runtime_config(update, actor, allow_private_origin, site_id=None)` scoped to one
    site and reusing the current validation/robots/bump-policy logic.
- Keeps `deposit_manager`, `session_manager`, `audit_logger`, `payment_provider`,
  `commerce`, `demo_runner`, `playground_runner` shared.

When active site changes or its config is applied, re-point shared services that hold a
site config: `deposit_manager.set_site_config`, `session_manager.set_site_config`,
`explainer.set_site_config`, `policy_assistant.set_site_config`, and
`demo_runner.set_max_deposit`.

### Per-site deposits and sessions

- `DepositManager` gets `set_site_config_resolver(callable)`; use the deposit's
  `site_id` to resolve the right `SiteConfig` for policy-version validation and the
  refund-policy checks (currently lines ~847, ~996, ~1235). Keep `_site_config` as the
  fallback for older records.
- `SessionRecord` gains `site_id: str = ""`. `SessionManager.issue_session` accepts
  `site_config: Optional[SiteConfig]` and `site_id: Optional[str]` and uses them for the
  limits it returns and for the stored record. Old sessions with an empty `site_id` are
  treated as belonging to the request's site.

## API contract

### Site management (`server/api/admin.py`)

All site-management endpoints use the same access rules as `/admin/config`
(`X-Admin-Token`, or public when `DEMO_CONFIG_PUBLIC=true` in demo mode). There is no
hourly write quota and no demo allowlist check.

`GET /admin/sites` →

```json
{
  "active_site_id": "www-example-com",
  "config_public": true,
  "sites": [{
    "site_id": "www-example-com",
    "name": "www.example.com",
    "host": "www.example.com",
    "origin_url": "https://www.example.com",
    "status": "live",
    "active": true,
    "origin_configured": true,
    "deposit_amount": "5.00",
    "currency": "USD",
    "allowed_paths": ["/"],
    "disallowed_paths": [],
    "rate_limit_requests": 60,
    "rate_limit_window_seconds": 60,
    "max_requests_per_session": 10,
    "policy_version": "1.0",
    "version": 0,
    "updated_at": "...",
    "origin_status": { "...OriginStatus..." }
  }]
}
```

`POST /admin/sites` body:

```json
{
  "origin_url": "https://example.com",
  "name": "Example",
  "set_active": true,
  "deposit_amount": null,
  "allowed_paths": [],
  "disallowed_paths": [],
  "rate_limit_requests": null,
  "rate_limit_window_seconds": null,
  "max_requests_per_session": null,
  "pricing_tiers": []
}
```

- `origin_url` required, absolute http(s), validated with `validate_origin_url` (public:
  `allow_private=False` + SSRF check, **no allowlist requirement**; admin:
  `allow_private=True`).
- New sites start with **no bundled demo paths and no pricing tiers** (`allowed_paths=[]`,
  `disallowed_paths=[]`, `pricing_tiers=[]`), `policy_version="1.0"`, and the configured
  global deposit/rate defaults. Origin robots.txt is imported and merged immediately.
- `set_active` defaults true.
- Returns the new site's `RuntimeConfigResponse`, HTTP 201.

`PUT /admin/sites/{site_id}` body `{"name": "...", "status": "live"|"paused"}` → updated
`SiteSummary`. `DELETE /admin/sites/{site_id}` → 204. `POST
/admin/sites/{site_id}/activate` → `SiteListResponse`.

### Config

- `GET /admin/config` and `PUT /admin/config` accept optional `?site_id=` (and
  `X-SARA-Site` header as an alternative). Default: active site.
- `POST /admin/config/test-origin` drops the allowlist requirement but keeps public SSRF
  checks.
- `POST /admin/config/sync-robots` is allowed for public config access too (not only
  admin) and accepts `?site_id=`.
- `RuntimeConfigResponse` adds `site_id`, `site_name`, `status`, `active_site_id`, `host`.
- Remove the `validate_demo_rate_limit` call and frontend cap. Remove the public config
  write quota check.

### Gateway routing

`server/api/gateway.py`:

- Resolve `SiteRuntime` per request: explicit `X-SARA-Site` header first, else host match,
  else active site.
- Use that runtime's `site_config`, `pricing`, `identity_service`, `policy_engine`,
  `origin_proxy`, `content_service`.
- After authenticating a session, if `session.site_id` is non-empty and differs from the
  resolved site, respond 403 `site_mismatch`.
- Keep `/gateway` and `/gateway/{path}` working unchanged for the active site.

`server/api/site.py`:

- Add explicit routes `/_site/{site_id}` and `/_site/{site_id}/{path:path}` that gate
  agents and proxy humans for that site, in both demo and gateway modes.
- Root/catch-all: resolve by host (gateway mode) / active (demo), and proxy humans to that
  site's origin. Add `_site` to reserved prefixes.
- A paused site returns 503 `{"error":"site_paused"}` for gateway traffic.

`server/api/policies.py`:

- `/policies` and `/robots.txt` resolve the site from `X-SARA-Site`, then `Host`, then
  active.

`server/api/sessions.py`:

- Look up the deposit first, resolve its site, validate `policy_version` against that
  site, and pass `site_config` + `site_id` to `issue_session`.

`server/api/playground.py`:

- `GET /playground/actions` accepts `?site_id=`, returns limits for that site.
- `PlaygroundRunRequest` gains `site_id: Optional[str]`. `start_run` resolves that site and
  passes its policy snapshot + version to the runner.
- `PlaygroundRunner.start(...)` accepts `site_id`; every HTTP request it makes carries
  `X-SARA-Site: <site_id>` so the selected console site is tested.

## Frontend contract (`server/static/dashboard.html`, `dashboard.js`, `dashboard.css`)

- Replace the disabled note in `#sites-card` with a real form (CSS `.site-add`,
  `.site-add-shell` already exists):

```html
<form class="site-add" id="site-add-form" autocomplete="off">
  <div class="site-add-shell">
    <span aria-hidden="true">https://</span>
    <input id="site-add-input" placeholder="example.com" spellcheck="false" aria-label="New website URL">
  </div>
  <button type="submit" class="primary" id="site-add-button">Add website</button>
</form>
```

- Load sites from `GET /admin/sites` (auth headers). Keep a localStorage fallback only for
  the selected id, not the whole site list.
- Selecting a site calls `POST /admin/sites/{id}/activate` (unless read-only), updates the
  active site, clears playground output, re-renders deposit/rate/rules, and reloads
  `/playground/actions?site_id=...`.
- Add website → `POST /admin/sites` with the URL; then refresh the list and select the
  new site. Show a banner on validation errors.
- Pause/resume → `PUT /admin/sites/{id}` with `status`. Remove → `DELETE /admin/sites/{id}`.
- Apply live → `PUT /admin/config?site_id=<id>` with deposit/allowed/disallowed/rate and
  (only if changed) `origin_url`. After success, update the site from the response.
- Playground run → include `"site_id": site.id` in `POST /playground/runs`.
- Delete all demo-cap code: `DEMO_RATE_LIMIT_*`, rate unit disabling, cap warning, the
  `site-add-button` forced-disable, and the “disabled for demo” note. Keep the rest of the
  minimal UI and existing pencil-to-edit behavior.
- In read-only mode (no token and `config_public` false), show the sites but disable writes.

## Tests

- Update existing expectations broken by the model change (`tests/test_gateway.py` site id,
  `tests/test_runtime_config.py` cap/allowlist/persistence assertions).
- Add `tests/test_multi_site.py` covering:
  1. `POST /admin/sites` twice; list shows both; active switches.
  2. `GET /admin/config?site_id=` returns that site's own rules and deposit.
  3. `/policies` respects `X-SARA-Site`.
  4. Gateway challenge/policy uses the `X-SARA-Site` site (different disallowed path).
  5. `/_site/{id}/...` gates agents and forwards humans to that site's origin.
  6. Demo rate limit above 10/min is accepted.
  7. Public config can add an arbitrary public origin without an allowlist; a private
     origin is still rejected for public writes.
  8. Delete a site and switch active site.
- Run `pytest -q` offline and keep the full suite green.
