# OpenSearch Cross-Cluster Replication POC

A local Docker Compose setup for testing OpenSearch cross-cluster replication (CCR) and DR switchover.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Normal State                                                   │
│                                                                 │
│  PRIMARY (leader)          SECONDARY (follower)                 │
│  localhost:9200            localhost:9201                        │
│  Dashboards :5601          Dashboards :5602                     │
│                                                                 │
│       ──── CCR replication ────►                                │
│                                                                 │
│  Indices: products, orders, users, events                       │
│  Auto-follow enabled on: products, events                       │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# Start both clusters, seed data, and set up replication
docker compose up -d

# Watch init logs (one-shot container)
docker logs -f opensearch-init
```

Startup takes ~2 minutes for health checks + replication bootstrap.

### Verify replication

```bash
# Check replication status for an index
curl http://localhost:9201/_plugins/_replication/products/_status | jq

# Count docs replicated to secondary
curl http://localhost:9201/products/_count | jq
```

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Two OpenSearch nodes + init container + two Dashboards |
| `indices.json` | List of indices to replicate, with optional per-index auto-follow config |
| `init/setup.sh` | Auto-runs on startup: creates indices, seeds data, sets up CCR |
| `dr-switchover.py` | Interactive DR testing & failback script (Python, AWS-compatible) |
| `find-missing-replication-indices.py` | Lists indices on the leader missing from the follower and prints curl commands to start replication |
| `config.json` | Shared config for both scripts (URLs, aliases, `credentials_secret_arn`, `verify_ssl`) |

## DR Switchover Script

Written in Python. Compatible with **AWS OpenSearch** (cross-cluster connections must be pre-configured in AWS before running — the script never registers remote clusters itself).

### Prerequisites

```bash
pip install -r requirements.txt   # requests, plus boto3 (only needed when credentials_secret_arn is set)
```

### Run

```bash
python3 dr-switchover.py                       # interactive menu
python3 dr-switchover.py --operation failover  # run failover directly
python3 dr-switchover.py --operation failback  # run failback directly
python3 dr-switchover.py --operation status    # show current replication state
```

Optional flags:

```bash
python3 dr-switchover.py \
  --primary-url    http://primary.example.com:9200 \
  --secondary-url  http://secondary.example.com:9200 \
  --indices-file   /path/to/indices.json \
  --primary-alias  primary-cluster    \
  --secondary-alias secondary-cluster \
  --secret-arn     arn:aws:secretsmanager:us-east-1:123456789012:secret:opensearch-creds-AbCdEf
```

Environment variable equivalents:

```bash
export OPENSEARCH_PRIMARY_URL=http://primary.example.com:9200
export OPENSEARCH_SECONDARY_URL=http://secondary.example.com:9200
export OPENSEARCH_INDICES_FILE=./indices.json
export OPENSEARCH_PRIMARY_ALIAS=primary-cluster      # alias on SECONDARY → PRIMARY
export OPENSEARCH_SECONDARY_ALIAS=secondary-cluster  # alias on PRIMARY   → SECONDARY
export OPENSEARCH_CREDENTIALS_SECRET_ARN=arn:aws:secretsmanager:...
```

Or use a `config.json` (see below).

### Credentials — AWS Secrets Manager

Both scripts read the OpenSearch username/password from a Secrets Manager secret instead of from `config.json`. Set `credentials_secret_arn` in `config.json` (or `--secret-arn` / `OPENSEARCH_CREDENTIALS_SECRET_ARN`). The same credentials are used for both clusters.

The secret must be a JSON string:

```json
{ "username": "admin", "password": "..." }
```

- AWS credentials come from the standard boto3 chain (`AWS_PROFILE`, SSO, instance role, …); the region is taken from the ARN.
- The caller needs `secretsmanager:GetSecretValue` on the secret (plus `kms:Decrypt` if it is encrypted with a customer-managed key).
- If the secret cannot be read (boto3 missing, bad ARN, no AWS credentials, access denied, secret not found, malformed or empty value) the script prints an `[ERR ]` line and exits with code 1 **before** any OpenSearch call — including in `--dry-run`.
- `OPENSEARCH_USERNAME` / `OPENSEARCH_PASSWORD`, when both set, override the secret (handy for ad-hoc runs).
- `username` / `password` keys in `config.json` are ignored with a warning.
- Leave the ARN empty for the local Docker setup (security disabled, no auth, boto3 not required).
- `find-missing-replication-indices.py` writes curl commands that reference `$OPENSEARCH_USERNAME` / `$OPENSEARCH_PASSWORD` rather than the resolved password; export those before running them.

### AWS Prerequisites — Connection Aliases

The script uses two pre-configured AWS OpenSearch cross-cluster connection aliases:

| Alias | Configured on | Points to | Used during |
|---|---|---|---|
| `primary-cluster` | SECONDARY domain | PRIMARY domain | FAILBACK |
| `secondary-cluster` | PRIMARY domain | SECONDARY domain | FAILOVER |

These are set up once via the AWS console (**Domains → Cross-cluster search → Create connection**).

### DR Workflow

Each operation is **self-contained** and can be run independently:

```
FAILOVER  (activate DR site)
──────────────────────────────────────────────────────────────────
  Step 1 ── Stop replication on SECONDARY + make indices R/W
              Delete auto-follow rules from SECONDARY (if any)
              (secondary is now the active cluster)

  Step 2 ── Start replication on PRIMARY  ←  SECONDARY
              Create auto-follow rules on PRIMARY (if any)
              (primary follows secondary during DR window)

      ← DR TESTING HAPPENS HERE →

FAILBACK  (return to normal)
──────────────────────────────────────────────────────────────────
  Step 1 ── Stop replication on PRIMARY + make indices R/W
              Delete auto-follow rules from PRIMARY (if any)
              (primary is again standalone)

  Step 2 ── Start replication on SECONDARY  ←  PRIMARY
              Create auto-follow rules on SECONDARY (if any)
              (normal state restored)
```

The script asks for confirmation before each step. Use `--non-interactive` to skip prompts (for automation/CI).

---

## Auto-Follow

Auto-follow lets the follower cluster automatically replicate new indices whose name matches a pattern — without manual intervention per index. It is configured per-index in `indices.json` using the `autofollow` boolean field.

### Configuration in `indices.json`

```json
{
  "indices": [
    { "name": "products", "description": "Product catalog",       "shards": 1, "replicas": 0, "autofollow": true  },
    { "name": "orders",   "description": "Customer orders",       "shards": 1, "replicas": 0, "autofollow": false },
    { "name": "users",    "description": "User profiles",         "shards": 1, "replicas": 0, "autofollow": false },
    { "name": "events",   "description": "Application event logs","shards": 1, "replicas": 0, "autofollow": true  }
  ]
}
```

- `autofollow: true` — the script creates an auto-follow rule on the follower cluster using the index `name` as the rule name and pattern. OpenSearch will automatically replicate any new index on the leader whose name matches the pattern.
- `autofollow: false` (or field absent) — index is replicated only via explicit `_start`, no auto-follow rule is created.
- **Auto-follow is fully automatic** — no separate CLI command is needed. The rules are created/deleted as part of normal failover and failback.

The `--operation status` output includes auto-follow stats from both clusters when any indices have `autofollow: true`.

---

## Retry Logic

`start_replication` calls use exponential backoff with jitter. If a replication start fails transiently, the script retries automatically before giving up.

Default: 3 attempts, base delay 2 s (delays: ~2 s, ~4 s, then gives up).

Override:

```bash
python3 dr-switchover.py --retry-max 5 --retry-delay 3.0 --operation failover
```

Or via environment variables:

```bash
export OPENSEARCH_RETRY_MAX=5
export OPENSEARCH_RETRY_DELAY=3.0
```

Or in `config.json`:

```json
{ "retry_max_attempts": 5, "retry_base_delay": 3.0 }
```

---

## Dry-Run Mode

Preview exactly what the script *would* do without touching either cluster. All mutating API calls (POST/PUT/DELETE) are replaced with log lines; GET calls (used for status checks) still execute normally.

```bash
# See what a failover would do
python3 dr-switchover.py --dry-run --operation failover --non-interactive

# Preview a failback
python3 dr-switchover.py --dry-run --operation failback --non-interactive
```

Each suppressed call is printed as:

```
[DRY-RUN]  POST http://localhost:9201/_plugins/_replication/products/_stop  {}
[DRY-RUN]  DELETE http://localhost:9201/_plugins/_replication/_autofollow  {"leader_alias": ...}
[DRY-RUN]  DELETE http://localhost:9200/products
[DRY-RUN]  PUT  http://localhost:9200/_plugins/_replication/products/_start  {...}
```

---

## Adding or Removing Indices

Edit `indices.json`. Set `"autofollow": true` on any index where you want the follower to automatically pick up new matching indices from the leader:

```json
{
  "indices": [
    { "name": "my-new-index", "description": "...", "shards": 1, "replicas": 0, "autofollow": false }
  ]
}
```

The `dr-switchover.py` script reads this file at runtime, so changes take effect immediately without restarting anything.

For the init seed data, add a corresponding `seed_<index>()` function and `create_index` + seed call in `init/setup.sh`.

## Teardown

```bash
docker compose down -v   # removes volumes (data) too
```
