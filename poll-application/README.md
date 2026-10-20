---
subtitle:   Poll application
author:     Victor TRENCIC
version:    0.1.0
---

A small voting application made of three services. Source and a `Dockerfile` per service are provided; building the images, pushing them to your registry, wiring the environment, and deploying on the cluster is your job.

## Architecture

```
poll --(RPUSH votes)--> Redis --(BLPOP votes)--> worker --(UPSERT)--> PostgreSQL <--(SELECT)-- result
```

- **poll** (Python/Flask) serves the voting page and pushes each vote as a JSON message onto the Redis list `votes`.
- **worker** (Java) pops votes from that Redis list and upserts them into the `votes` table in PostgreSQL.
- **result** (Node.js/Express + Socket.IO) reads the vote tally from PostgreSQL and pushes live updates to the browser over a websocket.

`schema.sql`, at the root of this directory, must be applied to the PostgreSQL database before the worker or the result service can do anything useful — it creates the `votes` table they both depend on.

Each service directory (`poll/`, `worker/`, `result/`) ships its own `Dockerfile`:

- `poll/Dockerfile` — Python 3.14 image running the app with gunicorn.
- `worker/Dockerfile` — multi-stage Maven build producing a Java 25 runtime image.
- `result/Dockerfile` — Node.js 24 image running the Express/Socket.IO server.

## Services

### poll

| | |
|---|---|
| Port | 80 |
| Health | `GET /healthz` (liveness), `GET /readyz` (200 if Redis answers `PING`, 503 otherwise) |

| Env var | Required | Default |
|---|---|---|
| `REDIS_HOST` | yes | - |
| `REDIS_PASSWORD` | no | none (no auth) |
| `OPTION_A` | no | `Ansible` |
| `OPTION_B` | no | `Chef` |
| `OPTION_C` | no | `Puppet` |
| `OPTION_D` | no | `SaltStack` |

### worker

| | |
|---|---|
| Port | 80 (health endpoints only, no application traffic) |
| Health | `GET /healthz` (liveness), `GET /readyz` (200 if Redis answers `PING` **and** the PostgreSQL connection is valid, 503 otherwise) |

| Env var | Required | Default |
|---|---|---|
| `REDIS_HOST` | yes | - |
| `REDIS_PASSWORD` | no | none (no auth) |
| `POSTGRES_HOST` | yes | - |
| `POSTGRES_PORT` | yes | - |
| `POSTGRES_DB` | yes | - |
| `POSTGRES_USER` | yes | - |
| `POSTGRES_PASSWORD` | yes | - |

### result

| | |
|---|---|
| Port | 80 |
| Health | `GET /healthz` (liveness), `GET /readyz` (200 if PostgreSQL answers `SELECT 1`, 503 otherwise) |

| Env var | Required | Default |
|---|---|---|
| `POSTGRES_HOST` | yes | - |
| `POSTGRES_PORT` | yes | - |
| `POSTGRES_DB` | yes | - |
| `POSTGRES_USER` | yes | - |
| `POSTGRES_PASSWORD` | yes | - |

## Backing services

This application expects:

- **Redis 8.x**. It may be protected with a password (`requirepass`); if so, set `REDIS_PASSWORD` above.
- **PostgreSQL 18**, with `schema.sql` applied to the target database beforehand.

Provisioning, securing and deploying Redis and PostgreSQL on the cluster is left to you.
