# Changelog

All notable changes to Privyx are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.15] - 2026-10-03

### Added

- **Docs:** the documentation is reorganized around what a reader wants to do: a quickstart, six tutorials, pages for frameworks and providers, and new pages on proxy modes, deployment, errors, limitations, and troubleshooting. The README is a short front page, and the project has a logo
- **Examples:** `examples/from_config.py` builds the engine from the configuration the CLI reads, and `plugins/detectors/iban.py` is a detector plugin that validates a checksum; the test suite runs both

### Changed

- **Examples:** `examples/openai.py` and `examples/anthropic.py`, which simulate a stream and call no provider, are now `examples/stream_restore_openai.py` and `examples/stream_restore_anthropic.py`

### Fixed

- **CLI:** `privyx run` printed its three lines about the proxy to stdout, ahead of the tool's own output, which broke a pipeline such as `privyx run claude -- -p … --output-format json | jq`; they now go to stderr
- **CLI:** with an IPv6 `host` such as `::1`, `privyx run` gave the tool a URL without brackets (`http://::1:PORT`), which no client can parse; the address is now bracketed
- **Detector:** the `llm` detector's default Anthropic model, `claude-3-5-haiku-latest`, has been retired, so every scan failed unless `llm_model` was set; the default is now `claude-haiku-4-5`
- **Docker:** `.env.example` copied to `.env` unchanged, as the README said to, gave the container a relative audit path it cannot write and a `localhost` upstream, so the proxy could not start; every line in the file is now commented out, and the image's and the compose file's defaults apply
- **Restore:** a token's id that the model wrote on its own (`9F3A1C2B7D4E5F60` for `<PRIVYX_EMAIL_9F3A1C2B7D4E5F60>`) was not restored, so a tool could receive the id in place of the value; an id of 12 characters or more is now restored too, for tokens the session issued

### Security

- **Proxy:** a request to a path that repeats `/v1` (`/v1/v1/messages`, what an Anthropic client calls when its base URL ends in `/v1`) was forwarded unmasked before the provider answered `404`; it is now masked like the route it means

## [0.1.14] - 2026-10-01

### Fixed

- **Restore:** a token the model wrote without its outer delimiters (`PRIVYX_EMAIL_1` for `<PRIVYX_EMAIL_1>`) was not restored, so a tool could receive the token's name in place of the value; that form is now restored too, for tokens the session issued
- **Proxy:** Codex spent 7 to 14 seconds at the start of every run retrying a WebSocket through the transparent proxy before it fell back to HTTP; a WebSocket upgrade is now answered `426`, on which Codex falls back at once

### Security

- **CLI:** `privyx run codex` with a `-c` of your own after a subcommand (`-- exec -c …`) bypassed the proxy and sent every request unmasked: codex then drops the base URL given before the subcommand. `privyx run` now says it again after your arguments

## [0.1.13] - 2026-10-01

### Added

- **Development:** `make e2e` can put a real provider behind its recorder (`--upstream`) and drive Codex as well (`--agent codex`); the script is now `scripts/test-e2e/`, one file per agent

### Security

- **Proxy coverage:** Claude Code's `safeguards` request field (working directory, home, user name, git branch and remotes) reached the provider unmasked; it is now masked like the rest of the request
- **Proxy coverage:** Codex's turn metadata (workspace paths and git remote URLs) reached the provider unmasked, in `client_metadata` and in the `x-codex-turn-metadata` request header; both are now masked, with the tokens the request body uses

## [0.1.12] - 2026-09-30

### Added

- **CLI:** `privyx run` warns when the tool exits without having sent a single request through the proxy, which is what a tool that calls its provider directly, unmasked, looks like
- **Development:** CI also runs the test suite on Python 3.15, with every extra but `presidio`, whose spaCy has no 3.15 build yet, and the package lists 3.15 as supported

### Changed

- **CLI:** `privyx run` writes its audit trail to `~/.local/state/privyx/audit.log` (`$XDG_STATE_HOME`) instead of `privyx-audit.log` in the directory it runs in, usually the project the tool works on, where the file could end up in a commit; `audit.path` and `PRIVYX_AUDIT_PATH` still override it

### Fixed

- **CLI:** `privyx run` dropped the tool's own key or login, so without `PRIVYX_<TYPE>_API_KEY` every request failed with `401` (`x-api-key header is required` for `privyx run claude`). It now follows `proxy.mode` like `privyx proxy`: the transparent proxy by default, which relays the tool's headers and paths (`--upstream` then uses only the URL's origin), or the gateway with `proxy.mode: gateway`. As with `privyx proxy`, a path outside `proxy.routes` is forwarded unmasked; `proxy.passthrough_unknown: false` refuses it
- **CLI:** `privyx run openai` and `privyx run aider` gave the tool a base URL without the `/v1` the OpenAI SDK expects, so every request was answered `404`
- **CLI:** a port outside 0–65535 (`port`, `PRIVYX_PORT`, `--port`) printed three tracebacks once the server tried to bind it; it is now refused at startup with an `Error:` message, and `privyx run` reports a port already in use in one line
- **CLI:** `privyx run` printed Privyx's warnings, such as an unreachable upstream, into the tool's terminal, over its screen; they now go to `log_file` when one is set, and nowhere else
- **CLI:** a `log_file` or `audit.path` that cannot be written stopped `privyx proxy` and `privyx run` with a traceback; it is now a one-line error, and a missing directory for `audit.path` is created

### Security

- **CLI:** `privyx run codex`, and `privyx run claude` when Claude Code's `settings.json` sets `ANTHROPIC_BASE_URL`, bypassed the proxy and sent every request unmasked: codex reads its base URL from its own config, and Claude Code prefers its settings to the environment. `privyx run` now passes the proxy's URL where each tool reads it first (`--settings`, and `-c openai_base_url=…` for codex's built-in OpenAI provider); give a router set in Claude Code's settings to `--upstream`
- **Proxy:** an OpenAI client whose base URL lacks `/v1` (`OPENAI_BASE_URL=http://localhost:8000`) calls `/chat/completions` and `/responses`, which the transparent proxy forwarded unmasked; these paths are now masked like their `/v1` versions
- **Vault:** the `sqlite` vault file with its `-wal` and `-shm` files and the `--map` file of `privyx mask`, which hold original values, were created readable by every local user, as was the anchor key of `privyx run` until its `chmod`; they are now created owner-only (`0600`). Existing files keep their mode (`chmod 600 privyx.db*`)

## [0.1.11] - 2026-09-29

### Changed

- **Config:** a boolean environment variable (`PRIVYX_AUDIT_ENABLED`, `PRIVYX_DETECTOR_CACHE`) that is not `1`/`true`/`yes`/`on` or `0`/`false`/`no`/`off` now stops `privyx` at startup instead of reading as false, so a typo such as `treu` no longer turns the audit trail off; an invalid `PRIVYX_PORT` fails too instead of falling back to 8000

### Fixed

- **CLI:** `privyx config` with an invalid configuration printed a traceback; it now prints a one-line `Error:` and exits 1 like the other commands
- **Vault:** creating a `sqlite` session was a read and then a write, so a conversation's 50 parallel first requests recorded 50 `session.created` events for one session, and a create could overwrite a session another request had just saved. It is now one statement, as with `redis`
- **Packaging:** the source distribution no longer ships three leftover test files (`scen.yaml`, `scen-px.log`, `scen-up.log`), and git and Docker builds ignore SQLite's `-wal` and `-shm` files, which hold an open vault's sessions, original values included

## [0.1.10] - 2026-09-29

### Added

- **Config:** `PRIVYX_VAULT_TTL` sets `vault.ttl`, and `docker-compose.yml` sets it to a week, so the `conversation` sessions it keeps in Redis, with their original values, expire after a week without a request instead of piling up forever

### Changed

- **Proxy:** `ephemeral` sessions (the default) no longer go through the vault: each lives in memory for its one request, so with a `sqlite` or `redis` vault a request makes no vault calls instead of six, and the original values it masks are never written to disk or Redis. `client`, `conversation`, and `x-privyx-session` sessions still live in the vault
- **Vault:** the `sqlite` vault writes through a write-ahead log (WAL), which more than halves its disk I/O (a session created, read, saved, read twice, and deleted: ~11 ms → ~4.5 ms), and sweeps expired sessions at most once a minute instead of on every new session. Keep its file on a local disk: WAL does not work on a network file system such as NFS

### Fixed

- **Proxy:** the detection cache kept every text it had scanned, so its 10,000 entries could hold ~500 MB of 50 KB prompts; it now keeps a digest of each text (1,000 such texts: 51 MB → 0.4 MB). The signed thinking text kept to answer echoed thinking blocks is capped at 16 million characters, where 4,096 blocks of 20 KB took 84 MB
- **Vault:** a `vault.dsn` file that is not a SQLite database stopped `privyx` with a traceback and then hung instead of exiting. It now exits with a one-line error, and every failed SQLite call is reported as a vault error
- **Vault:** with redis-py older than 8, a Redis that stopped answering held every request forever; the `redis` vault now gives up after 5 s with any version. The first request after a Redis restart no longer fails, creating a session is atomic, and every failed Redis call is reported as a vault error
- **Proxy:** when the session vault fails (Redis down or not answering, a SQLite error), both modes answer `503` with a `privyx_vault_unavailable` JSON error and record `proxy.error` with `phase: "vault"`, instead of a bare `500`. A stream's session is read before its `200` goes out, so a failure there is a `503` too, not a broken stream

## [0.1.9] - 2026-09-28

### Added

- **Development:** CI also runs the test suite, with every extra, on Python 3.13 and 3.14, which the package now lists as supported alongside 3.12

### Changed

- **Proxy:** a connection to the upstream that cannot be opened (refused, DNS, TLS handshake) is retried twice, 0.5 s and 1 s apart, before the client gets a `502`, so requests that arrive while the upstream restarts go through

### Fixed

- **Proxy:** with more than 100 requests in flight, the rest no longer wait for a free upstream connection and fail with a 500 after 10 s (a 502 in gateway mode): the pool is now unlimited. New settings `proxy.timeout`, `proxy.connect_timeout`, and `proxy.max_connections` set the upstream timeouts and pool size in both modes
- **Proxy:** a response with nothing to restore (a file download, anything on an unrouted path, a body that is not JSON) and an upload on an unrouted path were read whole before being passed on, so a 200 MB download reached the client only after its last byte (6 s instead of 0.005 s) and raised the proxy's memory by 400 MB. Both now stream through
- **Proxy:** stopping `privyx proxy` (SIGTERM, `docker stop`, Ctrl-C, a `--reload` restart) waited for every running request however long it took, so `docker stop` killed it after 10 s and left the sessions of running streams, with their original values, in a sqlite or redis vault. Running requests now get 5 s, then are cut off and their sessions deleted
- **Proxy:** when the upstream sends no response, the client gets a JSON error that says why instead of a bare 500 (transparent) or a 502 with an empty message (gateway): `502` when it cannot be reached, `504` when it times out, `503` with `Retry-After` when all `proxy.max_connections` are busy, and no traceback in the log. The gateway answers a body that is not a JSON object with `400` instead of `500`

### Security

- **Proxy:** a request to a masked path is no longer forwarded unmasked when its `Content-Type` is not JSON (`curl -d` sends a form type) or its body is not a JSON object: in transparent mode a JSON body is now masked whatever its `Content-Type`, and any other body gets a `400`

## [0.1.8] - 2026-09-28

### Fixed

- **Detector:** `CREDIT_CARD` masks only digit runs that pass the Luhn checksum, as every card number does, so millisecond timestamps, long IDs, and a `git log` hash next to its date are no longer masked (precision on the detection corpus 0.58 → 1.00, recall unchanged)
- **Detector:** `IP_ADDRESS` no longer masks addresses that point at no host: loopback (`127.0.0.1`), `0.0.0.0`, the documentation ranges (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`), and dotted numbers with an octet above 255 such as `999.1.2.300`. Private addresses are still masked (precision on the detection corpus 0.39 → 1.00, recall unchanged)
- **Detector:** `PHONE` no longer masks two bare numbers such as a screen size (`1920 1080`), the seconds and year of a `git log` date (`11 2026`), or an ID (`INV-2026-0042`): two digit groups now need a `+<country>` code, an area code in parentheses, or a leading 0, so a US local number such as `555-0142` is no longer masked. Numbers without separators are now found in E.164 form (`+6281234567890`) and when they start with 08 (`081234567890`) (on the detection corpus, precision 0.58 → 1.00 and recall 0.70 → 1.00)
- **Detector:** `SECRET` no longer masks placeholders and punctuation (`sk-...`, `-` in `${API_KEY:-}`), variables (`api_key=api_key`, `os.environ`), names that describe a secret (`TOKEN_URL`, `token_type`), docstring prose (`api_key: When set, …`), or lower-case `_key` names such as `cache_key` (precision on the detection corpus 0.43 → 1.00, recall unchanged). A config that copied `SECRET` from an older `configs/default.yaml` keeps the old pattern until you remove it

### Security

- **Detector:** values assigned to a secret-looking name (`DB_PASSWORD=…`, `api_key: …`, `"token": "…"`) are now masked without a config file. The `SECRET` pattern lived only in `configs/default.yaml`, which neither `pip install privyx` nor the Docker image loads. With `policy.type: strict` and your own `allowed` list, add `SECRET` to it

## [0.1.7] - 2026-09-28

### Added

- **Audit:** `proxy.request` records `transform_ms`, the part of the time to the upstream's headers spent masking the request, so the trail shows how much of a slow request is Privyx and how much the upstream

### Fixed

- **Proxy:** a request's session is read and written once instead of once per text leaf, so long conversations no longer slow down with the sqlite or redis vault (~1.5 s → ~0.03 s per request at 300 turns). Concurrent requests on one session no longer overwrite each other's pseudonyms
- **Detector:** the built-in `EMAIL` pattern took quadratic time on a long run of letters, digits, or dots without an `@`, so 80 KB of hex or base64 in a request froze the proxy, and every other request and stream, for ~5 s. It now stops at the RFC 5321 lengths and scans the same input in ~10 ms; valid addresses match as before
- **Operators:** masking or restoring a text with thousands of values took quadratic time, because the whole text was rebuilt once per value: 16,000 values in 0.9 MB took ~1 s each way, stalling every other request. The text is now built once (~50 ms each way)
- **Proxy:** a stream the client dropped, such as a Claude Code request cancelled with Esc, left its ephemeral session, with the original values, in a sqlite or redis vault: the deletion was cancelled along with the stream (80 of 80 dropped streams). It is now deleted, and the drop is recorded as `proxy.response` with `aborted: true`, where before no event was written
- **Gateway:** `privyx run` and `privyx proxy --gateway` gave up on the upstream after 60 s and answered 502, so a request whose first byte took longer (a long Claude Code turn, a busy upstream) failed. The gateway now uses the transparent proxy's timeouts: 10 s to connect, 300 s to read or write

### Security

- **Detector:** API keys, JWTs, private keys, `Bearer`/`Basic` credentials, and passwords in URLs are now masked without a config file. Their patterns lived only in `configs/default.yaml`, which neither `pip install privyx` nor the Docker image loads, so `privyx run` and `docker compose up` sent them upstream as-is. With `policy.type: strict` and your own `allowed` list, add `API_KEY`, `JWT`, `PRIVATE_KEY`, `AUTH_TOKEN`, and `URL_CREDENTIAL` to it, or they stay unmasked
- **Docker:** `docker-compose.yml` publishes the proxy on `127.0.0.1` only. It listened on every interface, so anyone on the network could send requests through it, with the upstream key if one was configured. To reach it from other hosts, put an authenticating reverse proxy in front

## [0.1.6] - 2026-09-27

### Added

- **Docs:** the documentation is published at [ohp1x.github.io/privyx](https://ohp1x.github.io/privyx/), built from `docs/` with MkDocs Material on every push to `main`. Pull requests that touch the docs build them with `--strict`, so a broken link or a page missing from the navigation fails the check. `make docs` serves the site locally
- **Docs:** a user guide: getting started, a configuration reference covering every setting and environment variable, detection (built-in patterns, secrets, word lists, Presidio, the LLM detector, policies), masking (operators, token format, anchors), sessions and vaults, a CLI reference, and examples for the OpenAI and Anthropic SDKs, curl, and coding tools. The example scripts and config files are embedded from `examples/` and `configs/`, so the pages show them as they are. Tests fail when a setting, environment variable, or CLI option is missing from its reference page, or when a script in `examples/` stops running

### Changed

- **Packaging:** `pip install privyx` now includes the proxy. FastAPI and uvicorn moved from the `server` extra into the base dependencies, so `privyx proxy` and `privyx run` work without an extra; before, they stopped with "requires privyx[server]", which the README's install line did not mention. The `server` extra is gone; `pip install privyx[server]` still installs everything, with a warning that the extra does not exist. The unused `sse-starlette` dependency is dropped

### Removed

- **Examples:** `examples/transparent_proxy.py`, a stub that proxied nothing and described a TLS-interception design Privyx does not use. `privyx proxy` is the transparent proxy

### Fixed

- **Config:** an unknown setting was ignored without a word, so a typo such as `term:` for `terms:` or `detectors:` for `detector:` left the values it listed unmasked while `privyx doctor` reported the configuration as valid. Unknown settings now stop Privyx at startup with their path and the closest valid name, never their value: `unknown setting 'detector.term' (did you mean 'terms'?)`. A config that carried keys Privyx ignored needs them removed
- **Plugins:** options in a plugin component's section (`detector: {type: license_plate, region: EU}`) never reached its `from_config`, which saw only the settings Privyx itself defines. They are now passed through in every family, providers included
- **Config:** `PRIVYX_DETECTOR_CACHE` replaced a `detector` list with one default regex detector, so the listed detectors (an `llm` or `presidio` detector, `terms`) silently stopped scanning and what only they caught went upstream unmasked. The variable now applies to each listed detector
- **Config:** a `yaml` detector written as a single mapping also detected the five built-in entities, although `yaml` is meant to use only the patterns it is given. The list form was not affected. `privyx config --show` no longer lists the built-in patterns under `detector.patterns`; the `regex` detector still adds them
- **Examples:** `examples/fastapi_gateway.py` passed an `HTTPProxy` to `Gateway`, which takes the provider, and failed with a `TypeError`
- **Docs:** the architecture overview's link to the testing guide was broken, and the streaming docs and several docstrings pointed to a design note that was never part of the repository

## [0.1.5] - 2026-09-27

### Added

- **Security:** `make vulns` checks every locked dependency against known advisories with `pip-audit`. CI runs it on every push and pull request and weekly, so a newly published advisory fails CI without a code change. Dependabot opens weekly update PRs for `uv.lock` (minor and patch grouped into one) and for GitHub Actions

### Changed

- **Detector:** the Anthropic LLM detector allows 4096 output tokens per call (was 1024), so the span list for a dense 4000-character chunk is not cut off, which would now fail the scan
- **Docs:** security issues and Code of Conduct violations are reported through GitHub's private vulnerability reporting. The `security@privyx.io` address the docs gave has no mailbox behind it, so reports sent there were lost

### Fixed

- **Detector:** an LLM reply with no JSON array in it (prose, an empty reply, or an array cut off at the token limit) used to count as "no PII found", so the text went upstream with only the other detectors' masking and no sign of it in the audit trail. It now fails the scan like a timeout: the request fails, or falls back to regex under `llm_fallback_on_error` and is counted as `llm_fallbacks`. An array wrapped in a code fence or prose is still read
- **Detector:** LLM spans are located by the text the model reports, not its character offsets. Models miscount characters, and an offset that still landed inside the text masked the wrong words and let the real value through. Every whole-word occurrence of the reported text, in the same case, is now masked (same case, so a name like "May" does not mask every "may"); reported text that does not occur is dropped. Offsets are used only for a span given without text
- **Proxy:** when masking a request fails (a detector error or timeout, including the LLM detector's fail-closed default), both proxy modes now answer `503` with a `privyx_scan_failed` JSON error and record `proxy.error` with `phase: "transform"`. The request was already never forwarded, but the client got a bare `500`, and neither the audit trail nor `/metrics` showed the failure. The console log gets the exception class only; the traceback goes to `log_file` at `DEBUG`
- **Gateway:** an ephemeral session is deleted even when masking fails part-way. The gateway created it inside the masking step, so after a failure it did not know the id, and any mappings made before the failure stayed in the vault until `vault.ttl` expired them
- **Detector:** entity types from the LLM are normalized: `person` and `phone number` become `PERSON` and `PHONE_NUMBER`, and a type that still cannot go into a token (empty, leading digit, over 64 characters) becomes `PII`. They used to be used verbatim, so `phone number` crashed the request and `person` got tokens separate from `PERSON`
- **CLI:** `privyx audit tail` keeps following when the log is moved away during rotation instead of crashing. The docs now say to rotate the audit log with `copytruncate`, since Privyx keeps the file open

## [0.1.4] - 2026-09-27

### Added

- **CLI:** `privyx audit stats [FILE] [--since 24h]` summarizes the audit log: requests, responses with average duration, errors by phase, sessions created and deleted, entities masked by type, and pseudonyms restored. `privyx audit tail [FILE] [-n N] [--no-follow]` prints recent events one readable line each and follows new ones, surviving a `copytruncate` rotation. `FILE` defaults to `audit.path`
- **Proxy:** `GET /metrics` in both proxy modes serves Prometheus-format counters of audit events, masked entities by type, errors by phase, restored pseudonyms, and response duration. They are counted in memory from startup, so they also work with `audit.enabled: false`. The endpoint needs no key, like `/health`
- **Detector:** `llm_fallback_on_error: true` lets requests through when the LLM detector fails or times out, scanned with the built-in regex patterns instead. It is off by default because the fallback misses what only the LLM catches (names, organizations). Each fallback is logged, and the result is never cached, so the next turn tries the LLM again
- **Audit:** `session.transform` carries `detector_counts` with `llm_calls`, estimated `llm_input_tokens` / `llm_output_tokens`, and `llm_fallbacks`, and is recorded even when the LLM found nothing to mask. `/metrics` serves them as `privyx_detector_counts_total` and `privyx audit stats` prints them. Plugin detectors can report their own counts through `Context.counters`

### Changed

- **Detector:** an LLM scan now fails the request with a `DetectorError` after `llm_timeout` seconds (default 30, covering every chunk), instead of waiting on the SDK's own minutes-long timeout. Raise `llm_timeout` if your scans legitimately take longer
- **Detector:** text longer than `llm_max_chars` (default 4000) is sent to the LLM in overlapping chunks, scanned concurrently, so long prompts no longer rely on one call returning every offset. Each chunk is its own LLM call

## [0.1.3] - 2026-09-27

### Added

- **Docker:** Docker Hub shows `docs/docker.md` as the image overview (quick start, environment variables, volumes, tags), synced by a workflow whenever the page changes. The README's `docker run` example now uses the public Docker Hub image
- **Vault:** `vault.ttl` now applies to the memory and SQLite vaults, not only Redis. A session idle longer than `ttl` seconds is no longer returned, and the SQLite vault deletes expired rows from disk whenever a new session is created, so sticky `client` / `conversation` sessions no longer pile up forever
- **CLI:** `privyx session list` shows every live session (id, last activity, creation time, mapping count, never values); `privyx session prune --older-than 7d [--dry-run]` deletes idle sessions and audits each one as `session.deleted` with `reason: prune`; `privyx session show <id>` replaces `privyx inspect session <id>`, which stays as an alias
- **Plugins:** vaults can implement `list_sessions()` to support `privyx session list` / `prune`. It is optional; a plugin vault without it keeps working and those two commands report that it cannot list sessions

### Changed

- **Vault:** a session's `updated_at` now records its last activity: every transform refreshes it, including one that adds no new mapping. It used to change only when a mapping was added, so a TTL could have expired a conversation that was still in use
- **Config:** `vault.ttl` must be a positive number of seconds. `ttl: 0` is now rejected at load time; Redis already refused it on the first write

### Fixed

- **Docker:** Images are built for `linux/arm64` as well as `linux/amd64`. v0.1.2 shipped amd64 only, so pulling it on Apple Silicon or an ARM server failed with "no matching manifest"
- **Docker:** The image no longer ships pytest, ruff, mypy, and the other development tools. `dev` is an optional extra, so `--all-extras` installed it despite `--no-dev`; the image now installs 14 fewer packages

## [0.1.2] - 2026-09-27

### Added

- **Docker:** Release tags now publish a Docker image to GHCR (`ghcr.io/ohp1x/privyx`), and to Docker Hub when the `DOCKERHUB_USERNAME` repo variable and `DOCKERHUB_TOKEN` secret are set (`DOCKERHUB_NAMESPACE` pushes to an organization instead of that user's namespace). Images are tagged `X.Y.Z` and `X.Y`; `latest` moves only on stable releases, never on rc/alpha/beta/dev tags
- **Config:** `PRIVYX_API_KEY` sets `provider.api_key`, the provider-independent counterpart of `PRIVYX_UPSTREAM_URL`. It works in both proxy modes and with `provider.type: generic`, where no per-provider variable applies
- **Docs:** `docs/providers/google.md` covers Gemini through its OpenAI-compatible endpoint in gateway and transparent mode, and lists what stays unmasked until a native provider lands
- **Development:** `make coverage` runs the suite with `pytest-cov` (terminal summary plus an HTML report in `htmlcov/`), and CI reports coverage on every run
- **Community:** `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1), issue forms for bug reports and feature requests (the bug form asks reporters to redact PII and keys, and security reports are routed to the security policy), and a pull request template

### Fixed

- **Proxy:** the transparent proxy resolves its upstream key like the gateway: `PRIVYX_<TYPE>_API_KEY` (`provider.<type>_api_key`) first, then `provider.api_key`. It used to read only `provider.api_key`, so `PRIVYX_OPENAI_API_KEY` / `PRIVYX_ANTHROPIC_API_KEY`, which `configs/default.yaml` pointed at, were ignored and the client's key was relayed instead. If you run the transparent proxy with `provider.type` set and one of those variables exported, that key now replaces the client's
- **Proxy:** `proxy.passthrough_unknown: false` now takes effect. The option was never read, so the transparent proxy forwarded unrouted paths (embeddings, Gemini-native `generateContent`, …) unmasked whatever it was set to. With `false`, it answers them with a 403 and forwards nothing
- **Docs:** the OpenAI and Anthropic provider pages configured `api_key: ${PRIVYX_*_API_KEY}`, which Privyx does not expand. In transparent mode that literal string replaced the client's key and the upstream answered 401. The pages now say where the key comes from

### Security

- **CLI:** `privyx config --show` no longer prints credentials. API keys, `operator.key`, `anchor.secret`, `tls.keyfile_password`, every `provider.headers` value, the values of `detector.patterns`, `detector.terms`, and `detector.llm_instructions` (which spell out what the detector hides; pattern and entity names stay visible), and the password in a URL field (`vault.dsn`, `vault.redis_url`, `provider.base_url`, `upstream_url`) are shown as `***`; unset values stay empty so a missing key is still visible. The upstream URL printed by `privyx config`, `privyx proxy`, and `privyx run` masks its password the same way

## [0.1.1] - 2026-09-27

### Added

- **Detectors:** `detector.type: llm` is now selectable. `LLMDetector` builds a client from the `providers` extra (`openai` or `anthropic` SDK) given `llm_provider` / `llm_model` / `llm_api_key`, or accepts a ready-made `client` for tests and custom endpoints. A missing SDK, an unsupported provider name, or a client the SDK itself refuses to construct (most commonly: no API key found) all fail at startup with a `ConfigError`, matching the `faker`/`encrypt`/`presidio` contract, never mid-request

### Fixed

- **Detectors:** `LLMDetector` could not be instantiated — it extended `BaseDetector`, which requires `detect_sync`, but every LLM call is inherently async, so there was no meaningful sync path (the same reasoning `CompositeDetector` follows by not extending it either). It now implements the `Detector` protocol directly
- **Detectors:** The LLM detector's response parser silently accepted spans with out-of-range offsets — Python slicing does not raise for an out-of-bounds `start`/`end`, it clamps — so a hallucinated offset outside the scanned text used to reach a `Span` unnoticed. Offsets are now validated (`0 <= start < end <= len(text)`) before a span is accepted

## [0.1.0] - 2026-09-26

First release: the privacy pipeline, the streaming proxy, and the CLI that drives them. Every component named in a config is pluggable through a registry, and the core stays free of FastAPI and provider SDKs.

### Added

- **Privacy engine:** `PrivacyEngine` orchestrating detect → policy → operate over a session vault (`core/engine.py`, `core/session.py`, `core/context.py`, `core/result.py`); `core/builder.py` assembles an engine from validated settings, so every knob in `configs/*.yaml` reaches a real component
- **Privacy engine:** Detectors: `regex` (layered over the built-in patterns) and `yaml` (exactly the patterns you list); Presidio and LLM detectors started as placeholders
- **Privacy engine:** Policies: `default` and `strict` (allow-list)
- **Privacy engine:** Operators: `pseudonym`, `redact`, `hash`
- **Privacy engine:** Anchors: `hmac` makes pseudonyms deterministic across sessions — the same value gets the same pseudonym under the same key. An empty `anchor.secret` means no anchoring rather than anchoring with a guessable key
- **Streaming:** `StreamingDeanonymizer`: trie plus hold-back buffer, so a pseudonym split across chunk boundaries is restored before the client sees any of it
- **Streaming:** Stateful `SSEDecoder` alongside the stateless `parse_sse`
- **Streaming:** Stream adapters for OpenAI and Anthropic envelopes, selected by name, with plain SSE as the fallback for custom endpoints; SSE envelope handling is kept separate from text transformation
- **Transport:** Vaults: `memory`, `sqlite` (optional extra), `redis` (optional extra) behind one `Vault` protocol — no pipeline state lives in process memory
- **Transport:** Providers: `generic`, `openai`, `anthropic`, resolved through a registry
- **Transport:** `HTTPProxy` connecting the engine to any provider; FastAPI gateway ships as the optional `[server]` extra
- **CLI:** `privyx proxy` — run the gateway
- **CLI:** `privyx run <tool>` — start a proxy on a free port and launch `claude`, `codex`, `openai`, or `aider` pointed at it
- **CLI:** `privyx detect` — inspect what the *configured* detector and policy see
- **CLI:** `privyx inspect session` — show a session's mapping, masked unless `--reveal`
- **CLI:** `privyx doctor` — checks, each exercising the configured component
- **CLI:** `privyx config` — show the resolved configuration
- **CLI:** `privyx proxy --reload` restarts the server when the config file changes, for tuning detectors and policies without a manual restart. Off by default; it is a restart rather than a hot swap, so in-flight requests finish and an in-memory vault starts empty again
- **CLI:** `privyx mask` / `privyx unmask` — pseudonymize and restore text without the proxy, for use in pipelines or from another project. Input comes from positional arguments, `--stdin`, or `-i FILE` (`-` for stdin); output goes to stdout or `-o FILE`. `-f text|json|jsonl` picks the walk (`auto` guesses from the input file's extension), JSON walks every string leaf and can be narrowed with `--path '$.messages'`, and non-string values are left untouched. The mapping lives either in a self-contained `--map FILE` — no vault, no deployment needed — or in the configured vault under `--session ID`. An existing map file is continued rather than overwritten, so a second document keeps the tokens the first one was given. Because the default vault is `memory`, `mask` warns when neither is in play and the output could never be unmasked
- **Configuration:** Precedence: built-in defaults < YAML file < environment
- **Configuration:** Upstream resolution: `upstream_url` < `provider.base_url` < the provider type's documented default
- **Configuration:** Shipped configs: `default`, `strict`, and examples for OpenAI, Anthropic, and a custom endpoint
- **Proxy:** Native TLS / HTTPS proxy support. The proxy server can now terminate TLS natively using `--ssl-certfile`, `--ssl-keyfile`, `--ssl-keyfile-password`, and `--ssl-ca-certs`, or via the new `tls` section in `config.yaml` / `PRIVYX_SSL_*` (`PRIVYX_TLS_*`) environment variables. When configured, both transparent and gateway proxy modes listen on `https://`
- **Performance:** Turn detection caching. In multi-turn chat sessions, LLM clients re-send the entire conversation history on every request, causing Privyx to redundantly scan past turns with all regex and NLP detectors on every single turn. A new `CachedDetector` implements an in-memory LRU cache (`detector.cache.enabled`, default: `true`, `detector.cache.max_size`, default: 10,000) for entity detections. Repeated text leaves from previous turns hit the cache in ~0.001 ms, speeding up request transformation in long conversations by over 100x. Can be disabled via `detector.cache: false` in YAML or `PRIVYX_DETECTOR_CACHE=false`
- **Proxy coverage:** OpenAI Responses API (`/v1/responses`, `/input_tokens`, `/compact`) is now routed with a new `responses` schema — the wire API Codex CLI speaks. Requests cover `instructions`, `input` (string or items: messages, function / custom / MCP / shell / apply-patch calls and their outputs, reasoning), and prompt variables. Streams restore every string `delta` per item and index, buffer tool-call input until its `.done`, and restore the full text repeated in `*.done`, `output_item.done`, and `response.completed`, keeping the `event:` lines. Works in transparent and gateway modes
- **Proxy coverage:** `session.strategy: conversation` fingerprints a Responses body on the first user message in `input`
- **Proxy coverage:** `scripts/e2e_claude.py` (`make e2e`): runs the real Claude Code CLI through `privyx run`, and with `--transparent` through `privyx proxy` on its defaults, against a recording fake Anthropic API in an isolated temp HOME. It fails on a planted canary reaching the upstream, a placeholder left unrestored, an echoed assistant turn that differs from what the upstream sent, or `system` / `tools` changing between turns
- **Proxy coverage:** Scope is now explicit: `/v1/embeddings`, the legacy `/v1/completions`, and Gemini-native paths are still forwarded verbatim. A gateway serving the default routes now also forwards `/v1/responses` to its single upstream endpoint; the token-counting and compaction routes stay unserved there (404) so a count never becomes a billed completion
- **Audit:** Ephemeral proxy sessions are now deleted from the vault after a batch response completes or a streaming response is drained/cancelled. A new `session.deleted` event records the safe `mapping_count` and reason only, and is written only after deletion succeeds. Header-based and derived `client`/`conversation` sessions remain available for continuity. Cleanup is best-effort: a vault deletion failure does not change a successful response or mask an existing stream/upstream error; it emits `proxy.error` with `phase: cleanup` and the exception class name, without a false deletion event
- **Audit:** New `proxy.response` records the completed exchange — total duration, batch `bytes` or streamed `frames`, and the `restored` count — complementing `proxy.request` (which stays the time-to-first-byte marker). Streaming responses now also emit `session.restore`; previously the streaming path bypassed the restore accounting entirely and recorded nothing
- **Audit:** New `proxy.error` records a failed exchange with a `phase` (`upstream` / `stream` / `response`) and the exception's **class name** — never its message, which could echo payload text. The "counts, not content" guarantee is documented in `docs/observability/audit-events.md`
- **Sessions:** New `session.strategy` (`PRIVYX_SESSION_STRATEGY`) decides how a session is identified when the client sends no `x-privyx-session` header — which Claude Code, codex, aider, and the OpenAI CLI never do, so every request used to become a fresh throwaway session. `ephemeral` (default) still mints one per request; `client` derives a stable session from the client credential (one API key → one session); `conversation` derives it from the credential and the first user message, so one conversation is one reused session while different conversations — even under the same key — stay isolated. Continuity is automatic: the id is derived before `get_or_create_session`, which already reuses a vault session on a hit. So a conversation's turns share one session and one pseudonym map, tokens stay stable across turns, and only the first turn logs `session.created`. Every derived id is keyed on the credential, so two callers can never share a map
- **Sessions:** `session.created` now records a `source` (`header` / `client` / `conversation` / `ephemeral`) next to the existing `client_supplied`, so the audit trail shows how each id was chosen. `get_or_create_session` also tolerates a concurrent create (clients fan out parallel requests at conversation start) instead of failing the race with a `VaultError`
- **Sessions:** `privyx run` is opinionated where the library stays neutral: it defaults to `conversation` and auto-provisions a persisted per-user HMAC anchor secret (`~/.config/privyx/anchor.key`, honoring `$XDG_CONFIG_HOME`), so pseudonyms are stable across turns *and* restarts out of the box. `--session-strategy` overrides the strategy, `--no-anchor` skips provisioning, and a user's own `session.strategy` / `anchor.secret` (config or env) still wins — `load_config` gained a low-precedence `base_extra` channel for exactly these caller defaults. `privyx config` and the proxy startup banner now show the active session strategy
- **Detectors:** A `detector.patterns` regex with a `(?P<value>...)` group masks only that group: `PASSWORD=(?P<value>\S+)` hides the secret but leaves the variable name readable, so a secret can be matched by the name it is assigned to. A match where the group did not take part masks the whole match
- **Detectors:** `detector.terms` takes a literal word list per entity — `PERSON: [ann, bob]` — instead of a hand-written regex. Privyx escapes each term, orders the longest first so a compound term wins over a substring of itself, and adds word boundaries only where the term ends in a word character, so a URL still matches whole. Matching is case-insensitive. `terms` and `patterns` are merged rather than exclusive; when both name the same entity the hand-written regex wins
- **Detectors:** `detector.type: presidio` is now selectable, translating Presidio entity names to the vocabulary the rest of Privyx speaks (`EMAIL_ADDRESS` → `EMAIL`, `PHONE_NUMBER` → `PHONE`, `US_SSN` → `SSN`, …; unlisted types keep their Presidio name). New config: `detector.language`, `detector.model` (default `{language}_core_web_sm`), `detector.entities` (empty → every recognizer), `detector.score_threshold` (default `0.35`). `presidio` is an optional extra (`pip install privyx[presidio]`); a missing package or a missing spaCy model fails at startup with a `ConfigError`, never mid-request, matching the `faker` and `encrypt` contract. Analyzers are cached per `(language, model)` so repeated builds do not reload spaCy. The `llm` detector remains unregistered
- **Detectors:** `detector` also takes a list of configs, run concurrently with their spans pooled — e.g. `regex` with `terms` alongside `presidio`, or two `presidio` languages. Overlaps are resolved after the policy, as for a single detector, so an allowed span is never folded into a dropped one; exact duplicates are collapsed so audit counts are not doubled. An empty list is rejected at startup
- **Operators:** New `FakerOperator` (`operator.type: faker`): replaces each detected span with a *realistic* fake of the same entity type (fake email, name, phone, SSN, …) instead of a token, so a model can reason over plausibly-shaped data. Reversible through the session vault; the same value fakes identically within a session and — via `operator.seed` — across runs, while `operator.locale` selects the Faker locale. Restoration is **literal**: a fake value is ordinary text with no delimiter, so reversal matches the exact substituted strings (leftmost, longest-match) rather than the token codec. Batch and streaming share that matching, so the `stream == batch` property still holds — `StreamRouter` now takes an injectable processor factory, and the transparent proxy and gateway pick the trie recognizer for a literal-restore operator (`stream_restore = "literal"`) and the codec recognizer otherwise. Documented trade-off: a fake value that also appears naturally in a response can be restored by coincidence — unlike the syntactically distinctive `<PRIVYX_…>` tokens. Prefer `pseudonym` when collision-free reversal matters more than realism. `faker` is an optional extra (`pip install privyx[faker]`); selecting it without the package fails at startup with a `ConfigError`, never mid-request
- **Operators:** New `EncryptOperator` (`operator.type: encrypt`): the first operator that keeps **no plaintext at rest**. It stores the AES-256-GCM *ciphertext* of each value in the session vault (not the original) and decrypts on restore, closing the at-rest gap `docs/security/cryptography.md` called out. Encryption is deterministic (SIV-style nonce derived from the value), so the same value maps to the same token — dedup and idempotent writes — while the GCM tag means a corrupted or foreign token is passed through untouched rather than restored to garbage. The token is an ordinary codec token (keyed-HMAC identifier), so streaming reuses the codec recognizer: batch and streaming share one decrypting resolver, and the `stream == batch` property is preserved and property-tested. A new optional `resolve` hook on the shared `restore` helper (and `Operator.build_resolver`) is the seam — no operator hard-codes how the vault value maps back to plaintext. `encrypt` is an optional extra (`pip install privyx[crypto]`) and needs a key (`operator.key` / `PRIVYX_ENCRYPT_KEY`, 64 hex chars); a missing package, a missing key, or a malformed key fails at startup, never mid-request
- **Plugins:** Added a local, opt-in plugin loader configured with `plugins.paths` (or `PRIVYX_PLUGIN_PATHS`); configured files and directories are imported without third-party entry points or `sys.path` mutation. Concrete subclasses of `BaseDetector`, `BaseOperator`, `BasePolicy`, `BaseProvider`, `BaseAnchor`, and `BaseVault` are auto-discovered and registered by their class-level `name`; optional `from_config` factories are supported. Added startup and shutdown lifecycle hooks, duplicate-name detection, and fail-fast `ConfigError` handling for missing paths, import failures, and startup-hook failures. Plugin types are available throughout the builders and CLI commands, while built-in types always take precedence. `privyx doctor` reports loaded plugin families and names. Added the `plugins/detectors/license_plate.py` example, plugin documentation, and loader/registry tests. Vault plugin types are now accepted by config
- **Observability:** New PII-safe audit trail (`observability/audit.py`): `AuditLogger` appends one JSON object per line — `session.created`, `transform`, `restore`, `proxy.request` — to a dedicated file (`audit.path`, default `privyx-audit.log`). It records entity **types and counts** and request metadata, never payload content, original values, or pseudonyms — enforced at the API (the `transform` helper takes a histogram, not spans). Audit events are emitted transport-agnostically: the engine reports privacy events, the transparent proxy and gateway report `proxy.request`. The logger is injected (disabled no-op by default), so `privyx doctor` and unit tests neither open nor write a file. Config: `audit.enabled` / `audit.path` (`PRIVYX_AUDIT_ENABLED`, `PRIVYX_AUDIT_PATH`). Writes are resilient — a failed write is logged and swallowed, never breaking a request
- **Token system:** New `token/` subsystem: a `LogicalToken` (namespace/type/identifier) plus a configurable `FormatCodec` that is the single source of truth for how tokens look in text (`token/model.py`, `token/codec.py`). Token syntax is now configuration (`token.format` / `token.namespace`, `PRIVYX_TOKEN_FORMAT`, `PRIVYX_TOKEN_NAMESPACE`). The default reproduces the existing `<PRIVYX_EMAIL_1>` output; alternatives such as `[[{namespace}:{type}:{id}]]` need no code change
- **Token system:** Streaming reversal is codec-driven (`streaming/recognizer.py`, `TokenStreamProcessor`): tokens split across arbitrary chunk boundaries are reconstructed as logical tokens, with `stream == batch` property-tested
- **Docker:** Docker and Docker Compose configuration. A multi-stage `Dockerfile` (builder + slim runtime, non-root user) and a `docker-compose.yml` wiring Privyx to a Redis-backed vault, so the proxy runs in a container out of the box
- **Tests:** 429 tests: unit, integration, and Hypothesis property tests. The streaming properties compare against the batch operator rather than the streaming path itself, and run over both pseudonym suffix styles — counters and anchor tokens

### Changed

- **Audit:** The audit trail gained a **versioned, correlated envelope** so it can back a long-lived reader (a store, query layer, or dashboard) without reshaping. Every line now carries `schema_version`, a human-readable ISO-8601 `time` (next to the epoch `ts`), and a `request_id` that ties every event of one proxied exchange together. Event names are consistent `<domain>.<action>`: `transform` and `restore` became `session.transform` / `session.restore`, joining `session.created`, `proxy.request`, and the new `proxy.response` / `proxy.error` / `session.deleted` events. **This is a breaking change to the on-disk format**, signalled by `schema_version: 1`
- **Audit:** Per-request aggregation. A request pseudonymizes many text leaves; the trail used to write one `transform` line per leaf. It now records a single `session.transform` (merged `entity_counts`, summed `transformations`) and a single `session.restore` per exchange, so the log reads one row per event that matters. Standalone library / `privyx mask` calls (outside a request) still emit per call
- **Detectors:** The spaCy NLP engine is configured explicitly (`detector.model`) instead of relying on Presidio's default, which quietly expects `en_core_web_lg` to be installed
- **Token system:** Operators build logical tokens and delegate serialization to the codec; `restore` locates tokens via the codec. No operator, the proxy, or storage hard-codes token syntax anymore
- **Token system:** `HashOperator` now emits the one configured token syntax instead of its own `HASH_…` placeholder
- **Observability:** A no-op `transform` or `restore` (zero replacements) is no longer recorded. Most of a proxied request is untouched text — system prompt, every content block, every tool result — and one line per skipped field buried the events that matter; `proxy.request` still records that the call happened

### Removed

- **Audit:** Dropped the redundant `client_supplied` field from `session.created` (its `source` already says how the id was chosen)
- **Token system:** Removed the unused `streaming/frontier.py`

### Fixed

- **Proxy coverage:** Requests are walked in cache-prefix order (`tools`, `system` / `instructions`, then the rest). Without an anchor, a value first seen in a later message used to renumber the system prompt's placeholders, so every turn missed the prompt cache
- **Detectors:** `detector.type: presidio` previously failed with `unknown detector type: presidio` even though the `PresidioDetector` class existed and its docstring told you to enable it via config — `build_detector` only knew `regex`, `yaml`, and plugins
- **Detectors:** An invalid entity name in `detector.patterns` or `detector.terms` is now rejected at startup instead of raising mid-request on the first detection. Names follow the token codec's grammar: a letter, then letters, digits, or underscores
- **Detectors:** The previous `detector` list handling, reachable only from code, merged regex patterns into one detector and silently ignored `presidio` and plugin items
- **CLI:** A config file with invalid YAML now raises a `ConfigError` with the parser's message instead of surfacing a raw `yaml` traceback
- **Observability:** `configure_logging`'s JSON formatter now serializes with `json.dumps` (the old placeholder embedded `%(message)s` in a JSON template, so any quote or newline corrupted the line). Re-configuring is idempotent

### Security

- **Detectors:** `configs/default.yaml` now ships secret patterns — the built-ins previously covered only EMAIL/PHONE/CREDIT_CARD/IP_ADDRESS/SSN, so API keys reached the upstream as-is. Adds `API_KEY` (vendor-prefixed keys: OpenAI, Anthropic, GitHub, AWS, Google, Slack, Stripe, …), `JWT`, `PRIVATE_KEY` (PEM/PGP, even cut off before `END`), `AUTH_TOKEN` (`Bearer`/`Basic`), `URL_CREDENTIAL` (`scheme://user:pass@host`), and `SECRET` (a value assigned to a secret-looking name in `.env`, YAML, JSON, code, query strings, or CLI flags)
- **Proxy coverage:** Many request and response fields bypassed the engine, so real PII reached the upstream or placeholders reached the client. Both directions now share one leaf walk instead of a walker per field. Restore walks *every* string leaf of a response (placeholders only exist because Privyx minted them). Transform walks the content subtrees (`messages`, `system`, `input`, `instructions`, `prompt`, `prediction`) and pseudonymizes every string there except opaque keys — ids and `*_id`, `type`, `role`, `name`, `signature`, `encrypted_*`, base64 `data` / `file_data`, URLs, `media_type`, `cache_control`, `status` — so a field added later is pseudonymized rather than leaked. Tool arguments are walked as parsed JSON, never as one text blob
- **Proxy coverage:** Tool and parameter `description` strings in `tools` are now pseudonymized (Anthropic, Chat, Responses). An MCP server writes them, so they can name a customer or an org, and they were forwarded raw on every turn. Everything else in `tools` (names, `enum`, `pattern`, `default`, `required`) is left untouched
- **Proxy coverage:** An echoed Anthropic `thinking` block now goes back upstream with the exact text the upstream signed. It was re-pseudonymized from the restored copy, and any PII-shaped value the model wrote itself (an email, a name) came back as a new placeholder, so the text no longer matched its signature. The upstream text is remembered by `signature` (per process, 4096 entries) from batch and streamed responses
- **Proxy coverage:** `/v1/messages/count_tokens` is routed with the `anthropic` schema — its body is a full Messages body and was previously forwarded raw, unmasked
- **Proxy coverage:** Anthropic `server_tool_use.input`, `document` blocks (plain-text `source.data`, `source.content`, `title`, `context`), `search_result`, `citations[].cited_text`, and code-execution `stdout` / `stderr` are now pseudonymized and restored; a streamed `citations_delta` is restored
- **Proxy coverage:** OpenAI Chat `refusal` (field and content part), the non-standard `reasoning` field (OpenRouter / vLLM / Ollama), legacy `function_call.arguments`, and `prediction.content` are covered in requests, batch responses, and streams; `reasoning` and `refusal` stream on their own buffers, and `function_call` is buffered like `tool_calls`
- **Proxy coverage:** Streams restore every non-delta event (`message_start`, `content_block_start`, finish/usage chunks, …) leaf by leaf, re-serializing only when something changed
- **Detectors:** Presidio entity names are translated to the vocabulary the rest of Privyx speaks. Without this, `policy: strict` would drop every Presidio span — its allow-list holds `EMAIL`, not `EMAIL_ADDRESS` — and forward the PII untouched

[Unreleased]: https://github.com/ohp1x/privyx/compare/v0.1.15...HEAD
[0.1.15]: https://github.com/ohp1x/privyx/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/ohp1x/privyx/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/ohp1x/privyx/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/ohp1x/privyx/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/ohp1x/privyx/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/ohp1x/privyx/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/ohp1x/privyx/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/ohp1x/privyx/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/ohp1x/privyx/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/ohp1x/privyx/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/ohp1x/privyx/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/ohp1x/privyx/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/ohp1x/privyx/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/ohp1x/privyx/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/ohp1x/privyx/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ohp1x/privyx/releases/tag/v0.1.0
