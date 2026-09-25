# Security status

**Posture:** single-user planning tool on a private network. No authentication
by design. Reviewed 2026-09-10; revised 2026-09-25 after an audit.

## Threat model

smolplan is meant to sit on a private network behind whatever already guards
that network. It holds no credentials, makes no outbound connections, and
stores nothing sensitive — team names, monthly FTE numbers, and initiative
names.

On such a network the realistic risks are a mistyped URL and a browser doing
something unexpected, not an attacker with a foothold.

"A browser doing something unexpected" turned out to be the real one. The
user's browser can reach the port and also visits the rest of the internet, so
two attacks need no login to ride on. A form or a body-less `fetch` from another
site is sent without a CORS preflight, and its side effect lands even though
its answer cannot be read: one `POST /api/reset` and fifty `POST /api/seed`
pushed the real plan off the bottom of the undo stack for good. And a page can
re-point its own hostname at the server's address (DNS rebinding), after which
the browser treats the app as that page's own origin and lets it read the
whole plan. `guard.py` closes both. If that ever stops
being true, the honest fix is a reverse proxy with OIDC in front, not
bolt-on auth in the app.

**There is no login.** Anyone who can reach the port can edit the plan. That
is a deliberate trade, recorded here so it is a decision rather than an
oversight.

## What is enforced

| Concern | Status | How |
|---|---|---|
| SQL injection | ✅ | Every query is parameterised. The two f-string SQL sites interpolate an allowlisted column name (`update_initiative`) and a hardcoded table tuple (`seed_fixture`) — never user input. |
| DNS rebinding | ✅ | Every request must name a host on the allowlist, `SMOLPLAN_ALLOWED_HOSTS` (default `localhost,127.0.0.1,[::1]`; a LAN deployment adds its own name or address). Anything else is a 400, on every route including `/health`. |
| Cross-site request forgery | ✅ | A write (anything but GET/HEAD/OPTIONS) is refused with 403 when the browser marks it as cross-site: `Sec-Fetch-Site` other than `same-origin` or `none`, or failing that an `Origin` that is not this host and port. Requests carrying neither — curl, scripts — are allowed; scripted access is intended. |
| Clickjacking and injected script | ✅ | Every response carries `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. No `unsafe-inline`: the page sets styles through the CSSOM, which the CSP does not govern, and a test fails if a style attribute is set directly. `/docs` and `/openapi.json` are not served. |
| Input validation | ✅ | Months must match `YYYY-MM` exactly, years 2000–2099, with no trailing newline — the start, end and deadline of an initiative, supply and reserve ranges, and the clock; an imported full date must be a real day (`31/02/2026` is refused, not read as February); FTE is an integer 0–10000 hundredths; demand offsets are 0–119; spans are capped at 240 months. Names must be non-empty *after* cleaning, so `"   "` is refused rather than stored as nothing; owner is at most 120 characters and notes at most 10,000 — each rides in all fifty undo snapshots. |
| Control and bidi characters | ✅ | Team, reserve and initiative names, owner, notes and imported names and references lose control characters (so no ANSI escape reaches a terminal that prints the export) and bidirectional overrides (so no name reads differently from how it is stored). Joiners are kept, so emoji survive; notes keep their newlines. Same rule as smoltask. |
| Ids that name nothing | ✅ | Checked before anything is written: an unknown team, reserve or initiative is a 404, not a foreign-key or 64-bit-overflow 500. `tests/test_robustness.py` asserts no hostile input returns a 5xx. |
| Team names the importer would confuse | ✅ | A team cannot be created or renamed to a name that matches an existing one ignoring case, spacing and punctuation — the way an import matches a column heading. A plan from before the rule that already holds such a pair has the ambiguous heading refused on import rather than guessed. |
| Undo history | ✅ | A request that changes nothing takes no step of the fifty-deep history — an unknown id is refused before its checkpoint, and a checkpoint the plan still matches afterwards is dropped — so junk requests cannot push real history off the end. |
| Mass assignment | ✅ | `update_initiative` writes only fields on an explicit allowlist. The stored `duration_m` is not on it: only `end_month` sets it, the path that checks it against the start and the deadline. |
| Path traversal | ✅ | Static files are served by Starlette's `StaticFiles`, which resolves and confines paths. Covered by tests. |
| Secrets in the tree | ✅ | None exist — the app has no API keys, tokens or passwords. A test scans for them anyway. |
| Database exposure | ✅ | `SMOLPLAN_DB` is deployment configuration, never user input; no route reflects it. |
| CSV formula injection | ✅ | The export prefixes any field starting with `=`, `+`, `-`, `@`, tab or CR with an apostrophe, so a name cannot become a formula when the file is opened in a spreadsheet. No ordinary initiative name starts with one of those, so nothing legitimate is altered. |
| SSRF | N/A | The app makes no outbound requests. |
| `amend=1` on the demand PUT | ⚠️ Accepted | It folds the write into whatever checkpoint is on top of the stack, so a client that sends it can make one change without a step of its own. Only the app's own page can send it from a browser (cross-site writes are refused), and it exists so one Save is one Ctrl+Z. |
| Request body size | ⚠️ Accepted | Every field is bounded by pydantic, and the import at 4 MB, but only once the body has been read. No cap in front of that. |
| Authentication | ⚠️ Accepted | None. See above. |
| Transport | ⚠️ Accepted | Plain HTTP on the LAN, like the rest of the fleet. |

## Container

- Runs as a non-root user (uid 10001 in the image; the compose file overrides to
  uid 1000 so it can write the bind-mounted database).
- Read-only root filesystem, `/tmp` on tmpfs, every capability dropped,
  `no-new-privileges`. Nothing writes anywhere but `/data`.
- Published on `127.0.0.1` unless `SMOLPLAN_BIND` says otherwise. Docker's
  published ports are opened ahead of ufw and firewalld, so a host firewall does
  not narrow a `0.0.0.0` binding; the default has to be the safe one.
- The base image is pinned by digest as well as tag.
- No outbound network access is required at runtime.
- Dependencies are pinned in `requirements.txt` and kept current by Dependabot
  with test-gated auto-merge. The automerge job holds a write token, so its
  actions are pinned to commits, it checks the PR's author and not only the
  actor, it tests on Python 3.10 and on the image's 3.14, and it builds and
  health-checks the image before merging — a docker bump is judged by something
  that ran it.
- The database lives on a bind mount at `./data`, outside the image.

## Tests

`tests/test_security.py` covers injection through every field the API accepts,
the column allowlist, month and FTE validation, static-path traversal, a secret
scan over the source tree, the host allowlist, cross-site write refusal, the
security headers, the text bounds and the stripping of control characters.
`tests/test_robustness.py` asserts that no hostile input returns a 5xx and that
requests which change nothing take no step of the undo history.
`tests/test_config.py` pins the compose, Dockerfile and workflow properties
above. Formula neutralisation in the export is covered in `tests/test_api.py`,
alongside the rest of its behaviour.

Run with `pytest -q` alongside the engine, API, import and frontend suites.
