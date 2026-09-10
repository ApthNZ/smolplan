# Security

## Read this first

smolplan has **no authentication**. There is no login, no session, no user
model. Anyone who can open the page can read and edit the entire plan. That is
a deliberate design choice for a single-user tool on a trusted network, and it
makes the app unsuitable for anything else.

**Do not expose it to the internet.** Run it on localhost, or on a private
network you control, or behind a reverse proxy that handles authentication for
it.

The code was written by an AI and has not been audited by a human. See the
warning at the top of the [README](README.md).

## Reporting something

If you find a vulnerability, please open an issue. Given the above, "there is
no authentication" is not a vulnerability — it is documented behaviour. Things
that *would* be worth reporting: injection through a validated field, a path
that escapes the static directory, a crash reachable from ordinary input, or
anything that lets a request read files it shouldn't.

This is a hobby project with no security team and no response-time commitment.

## What the code does enforce

Details, including the accepted risks, are in
[SECURITY_STATUS.md](SECURITY_STATUS.md). In summary: every SQL query is
parameterised, writes go through an explicit column allowlist, all input is
validated (month format, FTE bounds, offset bounds, span length), and static
files are served by a mount that confines paths. `tests/test_security.py`
covers each of those.
