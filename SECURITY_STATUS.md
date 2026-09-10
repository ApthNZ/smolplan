# Security status

**Posture:** single-user planning tool on a private network. No authentication
by design. Reviewed 2026-09-10.

## Threat model

smolplan is meant to sit on a private network behind whatever already guards
that network. It holds no credentials, makes no outbound connections, and
stores nothing sensitive — team names, monthly FTE numbers, and initiative
names.

On such a network the realistic risks are a mistyped URL and a browser doing
something unexpected, not an attacker with a foothold. If that ever stops
being true, the honest fix is a reverse proxy with OIDC in front, not
bolt-on auth in the app.

**There is no login.** Anyone who can reach the port can edit the plan. That
is a deliberate trade, recorded here so it is a decision rather than an
oversight.

## What is enforced

| Concern | Status | How |
|---|---|---|
| SQL injection | ✅ | Every query is parameterised. The two f-string SQL sites interpolate an allowlisted column name (`update_initiative`) and a hardcoded table tuple (`seed_fixture`) — never user input. |
| Input validation | ✅ | Months must match `YYYY-MM`; FTE is an integer 0–10000 hundredths; demand offsets are 0–119; spans are capped at 240 months; names are length-bounded by pydantic. |
| Mass assignment | ✅ | `update_initiative` writes only fields on an explicit allowlist. |
| Path traversal | ✅ | Static files are served by Starlette's `StaticFiles`, which resolves and confines paths. Covered by tests. |
| Secrets in the tree | ✅ | None exist — the app has no API keys, tokens or passwords. A test scans for them anyway. |
| Database exposure | ✅ | `SMOLPLAN_DB` is deployment configuration, never user input; no route reflects it. |
| SSRF | N/A | The app makes no outbound requests. |
| CSRF | ⚠️ Accepted | No cookies and no auth, so there is no session to ride. If auth is ever added, CSRF protection must be added with it. |
| Authentication | ⚠️ Accepted | None. See above. |
| Transport | ⚠️ Accepted | Plain HTTP on the LAN, like the rest of the fleet. |

## Container

- Runs as a non-root user (uid 10001 in the image; the compose file overrides to
  uid 1000 so it can write the bind-mounted database).
- No outbound network access is required at runtime.
- Dependencies are pinned in `requirements.txt` and kept current by Dependabot
  with test-gated auto-merge.
- The database lives on a bind mount at `./data`, outside the image.

## Tests

`tests/test_security.py` — 22 tests covering injection through every field the
API accepts, the column allowlist, month and FTE validation, static-path
traversal, and a secret scan over the source tree.

Run with `pytest -q` alongside the engine and API suites.
