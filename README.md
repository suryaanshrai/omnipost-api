# omnipost-api

The REST API and job engine behind [OmniPost](https://github.com/suryaanshrai/omnipost):
write a post once, publish it to eleven networks, and see exactly what landed.

Django 5.2 + Django REST Framework, Postgres, Redis + RQ for background jobs.
The web app is [omnipost-ui](https://github.com/suryaanshrai/omnipost-ui); the
production compose stack lives in [omnipost](https://github.com/suryaanshrai/omnipost).

## Run it locally

Needs Python 3.12, Postgres 16 and Redis 7. The quickest way to get the two
stores is the dev compose file in the `omnipost` repo (`make dev`), or:

```bash
docker run -d --name omni-pg -e POSTGRES_USER=root -e POSTGRES_PASSWORD=root -e POSTGRES_DB=omni-db -p 5432:5432 postgres:16
docker run -d --name omni-redis -p 6379:6379 redis:7-alpine
```

Then:

```bash
pip install poetry==1.8.5 poetry-plugin-export==1.8.0
poetry install --with dev        # pygraphviz needs graphviz headers; skip with --without dev if you only run the app
export DJANGO_DEBUG=true DJANGO_SECRET_KEY=dev OMNIPOST_KEK=$(openssl rand -base64 32) \
       DB_HOST=localhost DB_NAME=omni-db DB_USER=root DB_PASSWORD=root REDIS_HOST=localhost
cd app
poetry run python manage.py migrate
poetry run python manage.py runserver 8000                       # the API
poetry run python manage.py rqworker --with-scheduler publish maintenance default media metrics ai   # the worker
poetry run python manage.py run_scheduler                        # periodic tasks
```

Emails (password reset, verification, failure alerts) print to the console
unless `EMAIL_HOST` is set.

## Tests and checks

```bash
poetry run pytest                 # against Postgres if DB_HOST is set, else in-memory SQLite
poetry run ruff check .
poetry run mypy app
cd app && poetry run python manage.py spectacular --file ../schema.yml   # keep schema.yml in sync
```

CI runs all of these on every pull request, plus `makemigrations --check`
and `pip-audit`.

### Live checks against real accounts

The normal suite mocks every platform. To check that your own accounts and
tokens work, use the live tests (never run in CI):

```bash
cp .env.live.example .env.live     # fill in the platforms you have
./scripts/live_test.sh             # metrics read for each platform with credentials
./scripts/live_test.sh -k mastodon # one platform
LIVE_PUBLISH=1 ./scripts/live_test.sh -k bluesky   # also publishes a real test post
```

Platforms without credentials are skipped, with the variable to set. By
default the tests only read: they fetch metrics for a post you already
have. `LIVE_PUBLISH=1` publishes a short "OmniPost live test" post, and
`LIVE_FIRST_COMMENT=1` replies to it. Delete those posts afterwards.

Analytics on LinkedIn, Threads, YouTube and Instagram reach need extra
OAuth scopes. Set `LINKEDIN_EXTRA_SCOPES`, `THREADS_EXTRA_SCOPES`,
`YOUTUBE_EXTRA_SCOPES` or `META_EXTRA_SCOPES` (see the omnipost repo's
`.env.example`), get the scope approved for your app, then reconnect the
channel.

## How it fits together

```
Organization ─┬─ Workspace ─┬─ Membership (user + role)
              │             ├─ Channel ── Credential (encrypted)
              │             ├─ MediaAsset
              │             └─ Post ── PostTarget (one per channel) ── PublishAttempt
```

- A **Post** is the idea; each **PostTarget** is what goes to one channel
  (its own text, media, thread parts). Scheduling a post creates one
  **PublishAttempt** per target and queues it.
- `jobs/publisher.py` runs an attempt: claim it (committed as RUNNING),
  call the platform outside any transaction, record the result. A RUNNING
  attempt is never re-run; if its worker died, the reconciler marks it
  "unknown outcome" for the user to check. Retries resume from the last
  saved step.
- `run_scheduler` (one process) reconciles lost or stuck jobs, fires
  recurring posts, refreshes expiring tokens and queues metrics polls.
- Each platform is a connector in `app/connectors/platforms/`, implementing
  `connectors.base.Connector` (`validate`, `publish`, optional OAuth,
  `refresh`, `fetch_metrics`).
- Credentials and AI keys are envelope-encrypted with `OMNIPOST_KEK`
  (`omnipost_api/crypto.py`). Back that key up: without it, connected
  accounts can't be decrypted.

## Signing in from scripts

Make a personal API token in the app (Settings → API tokens) or with
`POST /account/tokens/ {"name": "...", "read_only": false, "expires_in_days": 90}`,
then send `Authorization: Bearer op_...`. A token acts with your
permissions (read-only tokens can only GET). It can't make or revoke
tokens, change your password or delete the account.

The web app signs in with an httpOnly cookie instead of keeping the token
in localStorage. It sends `X-OmniPost-Client` with every request; the
cookie is ignored without it, which keeps other sites from using it. If
the app and API are on different sites (not just subdomains), set
`AUTH_COOKIE_SAMESITE=None` (needs HTTPS).

`mcp_server/` is an MCP server built on this: see its README to post from
Claude or any MCP client.

## Permissions

Every row belongs to a workspace, and every request is scoped to the
caller's workspaces. Writes need a role in that workspace:

| Role | Can |
|---|---|
| viewer | read |
| client_reviewer | read, approve or send back posts in review |
| contributor | draft posts, upload media, use AI |
| editor | publish, schedule, queue, cancel; voice profiles, queue slots, rules |
| admin | members, channels, app credentials, AI keys |
| owner | everything, including deleting the workspace |

## Layout

| Path | What's there |
|---|---|
| `app/omnipost_api/` | Models, serializers, views, permissions, services |
| `app/connectors/` | One connector per platform, the shared base, `netguard` (SSRF guard) |
| `app/jobs/` | Publisher, scheduler, reconciler, token refresh, metrics, media, notifications |
| `app/ai/` | AI providers, prompts, quota |
| `schema.yml` | OpenAPI schema; the UI generates its types from it |
| `docker/entrypoint.sh` | Process roles: `web`, `worker`, `worker-publish`, `worker-media`, `scheduler`, `migrate` |

## License

GPL-3.0. See [LICENSE](LICENSE).
