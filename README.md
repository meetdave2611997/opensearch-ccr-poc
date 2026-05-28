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
| `indices.json` | List of indices to replicate (edit to add/remove indices) |
| `init/setup.sh` | Auto-runs on startup: creates indices, seeds data, sets up CCR |
| `dr-switchover.py` | Interactive DR testing & failback script (Python, AWS-compatible) |

## DR Switchover Script

Written in Python. Compatible with **AWS OpenSearch** (cross-cluster connections must be pre-configured in AWS before running — the script never registers remote clusters itself).

### Prerequisites

```bash
pip install requests
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
  --primary-alias  primary-cluster   \
  --secondary-alias secondary-cluster
```

Environment variable equivalents:

```bash
export OPENSEARCH_PRIMARY_URL=http://primary.example.com:9200
export OPENSEARCH_SECONDARY_URL=http://secondary.example.com:9200
export OPENSEARCH_INDICES_FILE=./indices.json
export OPENSEARCH_PRIMARY_ALIAS=primary-cluster      # alias on SECONDARY → PRIMARY
export OPENSEARCH_SECONDARY_ALIAS=secondary-cluster  # alias on PRIMARY   → SECONDARY
```

Or use a `config.json` (see below).

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
              (secondary is now the active cluster)

  Step 2 ── Start replication on PRIMARY  ←  SECONDARY
              (primary follows secondary during DR window)

      ← DR TESTING HAPPENS HERE →

FAILBACK  (return to normal)
──────────────────────────────────────────────────────────────────
  Step 1 ── Stop replication on PRIMARY + make indices R/W
              (primary is again standalone)

  Step 2 ── Start replication on SECONDARY  ←  PRIMARY
              (normal state restored)
```

The script asks for confirmation before each step. Use `--non-interactive` to skip prompts (for automation/CI).

## Adding or Removing Indices

Edit `indices.json`:

```json
{
  "indices": [
    { "name": "my-new-index", "description": "...", "shards": 1, "replicas": 0 }
  ]
}
```

The `dr-switchover.py` script reads this file at runtime, so changes take effect immediately without restarting anything.

For the init seed data, add a corresponding `seed_<index>()` function and `create_index` + seed call in `init/setup.sh`.

## Teardown

```bash
docker compose down -v   # removes volumes (data) too
```
