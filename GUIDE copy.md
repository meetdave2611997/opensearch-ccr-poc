# OpenSearch DR Switchover — User Guide

This guide is for people who have **never used this tool**. It explains the ideas first, then the config file, then every command you will run.

The script you use is `[dr-switchover.py](dr-switchover.py)`. It failover and failback two OpenSearch clusters that already have **cross-cluster replication (CCR)** set up.

---

## 1. What this is?

You have two OpenSearch clusters:


| Role          | Typical name        | Local Docker URL        | Meaning                                                                |
| ------------- | ------------------- | ----------------------- | ---------------------------------------------------------------------- |
| **PRIMARY**   | Production / leader | `http://localhost:9200` | The cluster that applications write to in the **normal** state         |
| **SECONDARY** | DR / follower       | `http://localhost:9201` | A standby copy. It follows PRIMARY. Follower indices are **read-only** |


**Cross-cluster replication (CCR)** copies index data from the leader to the follower.

**Failover** = PRIMARY is down or you want to test DR. SECONDARY becomes the writable leader. PRIMARY (if it is still up) starts following SECONDARY.

**Failback** = DR test is over. PRIMARY becomes the writable leader again. SECONDARY follows PRIMARY.

```
NORMAL (before failover)
  PRIMARY (leader, R/W)  ──CCR──►  SECONDARY (follower, read-only)

AFTER FAILOVER
  SECONDARY (leader, R/W)  ──CCR──►  PRIMARY (follower, read-only)

AFTER FAILBACK
  PRIMARY (leader, R/W)  ──CCR──►  SECONDARY (follower, read-only)
```

The script never creates the AWS/OpenSearch **connection aliases**. Those must already exist. Locally, Docker `init/setup.sh` creates them for you.

---



## 2. Files in this folder


| File                                                                         | What it is                                                                     |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `[dr-switchover.py](dr-switchover.py)`                                       | The DR tool. Failover, failback, status.                                       |
| `[indices.json](indices.json)`                                               | The list of indices and auto-follow rules the script manages.                  |
| `[docker-compose.yml](docker-compose.yml)`                                   | Local PRIMARY + SECONDARY + Dashboards + init.                                 |
| `[init/setup.sh](init/setup.sh)`                                             | First-time seed: creates indices, registers aliases, starts CCR.               |
| `[requirements.txt](requirements.txt)`                                       | Python dependency (`requests`).                                                |
| `[find-missing-replication-indices.py](find-missing-replication-indices.py)` | Optional helper: find indices on the leader that are missing on the follower.  |
| `[generate_fake_indices.py](generate_fake_indices.py)`                       | Optional helper: create sample `sample-idx-*` indices for auto-follow testing. |
| `[README.md](README.md)`                                                     | Short project overview. This GUIDE is the full walkthrough.                    |


---



## 3. Concepts you must know before editing `indices.json`

The script splits every entry in `indices.json` into **exactly one** of two buckets:

### Static index (explicit CCR)

- Has `"name"` and `"autofollow": false` (or no `autofollow` field).
- The script **stops, deletes, and starts replication by that exact name**.
- Use this for a small, known set of indices (`orders`, `users`, `events`).



### Auto-follow rule

- Has `"autofollow": true` **and** a required `"autofollow_pattern"`.
- OpenSearch watches the leader and **automatically starts replication** for any new index whose name matches the pattern (for example `products`* → `products`, `products-1`, `products-2`).
- The script does **not** call explicit `_start` for those matching indices. The rule does that.
- If an entry also has `"name"`, that name is only the **rule identifier** (no `*` or `?` allowed). It is **not** treated as a static index.

**An index cannot be both.** If `autofollow` is `true`, it is auto-follow only — even if `name` is present.

### Why auto-follow needs `autofollow_pattern`

`name` is an identifier. `autofollow_pattern` is the wildcard OpenSearch matches.

```
name:               products          ← rule name sent to the API (no wildcards)
autofollow_pattern: products*         ← matches products, products-1, products-2024, …
```

If you set `"autofollow": true` without `autofollow_pattern`, the script prints an error and **exits**.

---



## 4. `indices.json` structure

Top level is always:

```json
{
  "indices": [ ... entries ... ]
}
```



### Fields


| Field                 | Required?                                    | Used for                                            | Notes                               |
| --------------------- | -------------------------------------------- | --------------------------------------------------- | ----------------------------------- |
| `name`                | Static: **yes**. Auto-follow: optional       | Static index name, **or** auto-follow rule name     | Must not contain `*` or `?`         |
| `autofollow`          | No (default `false`)                         | Route: static vs auto-follow                        | `true` = auto-follow only           |
| `autofollow_pattern`  | **Required when** `autofollow` **is** `true` | Wildcard matched on the leader                      | Example: `products`*, `logs-2026-*` |
| `description`         | No                                           | Human note only                                     | Ignored by the script               |
| `shards` / `replicas` | No                                           | Used by local `init/setup.sh` when creating indices | Ignored by `dr-switchover.py`       |




### Shape A — Static index (explicit replication)

```json
{
  "name": "orders",
  "description": "Customer orders",
  "shards": 1,
  "replicas": 0,
  "autofollow": false
}
```

What the script does:

- Failover Step 1: stop CCR + remove write blocks on SECONDARY for `orders`.
- Failover Step 3: delete `orders` on PRIMARY, then `_start` replication from SECONDARY.
- Failback does the reverse.



### Shape B — Named auto-follow rule (recommended)

Use this when you have a family of indices and want a readable rule name.

```json
{
  "name": "products",
  "description": "Product catalog family",
  "autofollow": true,
  "autofollow_pattern": "products*"
}
```

What the script does:

- Does **not** put `products` in the static index list.
- Creates/deletes one OpenSearch auto-follow rule named `products` with pattern `products*`.
- On rule delete: finds every live index matching `products*` on that cluster, stops CCR, removes write blocks, then deletes the rule.
- On role change: discovers matching indices on the new follower, includes them in the destructive delete list, then recreates the rule so OpenSearch re-follows them.



### Shape C — Auto-follow only (no `name`)

Use this when there is no single “base” index — only a pattern.

```json
{
  "description": "Metrics index family (metrics-idx-01 … metrics-idx-50)",
  "autofollow": true,
  "autofollow_pattern": "metrics-idx*"
}
```

The rule name is derived by stripping `*` and `?` from the pattern:

`metrics-idx*` → rule name `metrics-idx`

### Mixed file (the usual production case)

```json
{
  "indices": [
    {
      "name": "orders",
      "description": "Customer orders — explicit CCR",
      "autofollow": false
    },
    {
      "name": "users",
      "description": "User profiles — explicit CCR",
      "autofollow": false
    },
    {
      "name": "events",
      "description": "Event logs — explicit CCR",
      "autofollow": false
    },
    {
      "name": "products",
      "description": "Product family — auto-follow",
      "autofollow": true,
      "autofollow_pattern": "products*"
    },
    {
      "description": "Metrics family — auto-follow only",
      "autofollow": true,
      "autofollow_pattern": "metrics-idx*"
    }
  ]
}
```

How the script reads that file:


| Bucket                                           | Entries                                                  |
| ------------------------------------------------ | -------------------------------------------------------- |
| Static indices (`--operation` Step 1 / `_start`) | `orders`, `users`, `events`                              |
| Auto-follow rules                                | `products` → `products*`, `metrics-idx` → `metrics-idx*` |


At least one static index **or** one valid auto-follow rule is required. An empty file is an error.

---



## 5. One-time setup



### Local Docker (learning / POC)

You need Docker Desktop (or Docker Engine + Compose).

```bash
cd infra/opensearch-ccr-poc

# Start both clusters + Dashboards + init
docker compose up -d

# Watch first-time setup (creates indices, aliases, starts CCR)
docker logs -f opensearch-init
```

Wait until setup prints `Setup complete!` (~2 minutes).


| Service              | URL                                            |
| -------------------- | ---------------------------------------------- |
| PRIMARY OpenSearch   | [http://localhost:9200](http://localhost:9200) |
| SECONDARY OpenSearch | [http://localhost:9201](http://localhost:9201) |
| PRIMARY Dashboards   | [http://localhost:5601](http://localhost:5601) |
| SECONDARY Dashboards | [http://localhost:5602](http://localhost:5602) |


Init registers these aliases (the script expects the same names by default):


| Alias               | Created on | Points to | Used during                          |
| ------------------- | ---------- | --------- | ------------------------------------ |
| `primary-cluster`   | SECONDARY  | PRIMARY   | Failback (SECONDARY follows PRIMARY) |
| `secondary-cluster` | PRIMARY    | SECONDARY | Failover (PRIMARY follows SECONDARY) |


Tear down (destroys data):

```bash
docker compose down -v
```



### AWS OpenSearch (real environments)

Do this **once** in the AWS console before running the script:

1. Open **OpenSearch → Domains → Cross-cluster search / connections**.
2. On the **SECONDARY** domain, create an outbound connection to PRIMARY. Alias: `primary-cluster` (or whatever you pass as `--primary-alias`).
3. On the **PRIMARY** domain, create an outbound connection to SECONDARY. Alias: `secondary-cluster` (or `--secondary-alias`).
4. Accept both connections.
5. Put both domain endpoints and the same aliases into CLI flags, env vars, or `config.json`.

The script will **not** create or accept those connections.

### Python environment

```bash
cd infra/opensearch-ccr-poc
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---



## 6. How to run the script

```bash
cd infra/opensearch-ccr-poc
source .venv/bin/activate

# Interactive menu (recommended the first time)
python3 dr-switchover.py

# Direct operations
python3 dr-switchover.py --operation status
python3 dr-switchover.py --operation failover
python3 dr-switchover.py --operation failback
```

Menu choices:


| Choice | Operation                                             |
| ------ | ----------------------------------------------------- |
| 1      | Failover — activate DR site                           |
| 2      | Failback — restore normal state                       |
| 3      | Status — health, CCR, write blocks, auto-follow stats |
| 4      | Quit                                                  |




### Pointing at real clusters

```bash
python3 dr-switchover.py \
  --primary-url    https://search-primary.example.com \
  --secondary-url  https://search-secondary.example.com \
  --indices-file   ./indices.json \
  --primary-alias  primary-cluster \
  --secondary-alias secondary-cluster \
  --operation status
```



### Configuration precedence (highest wins)

1. CLI flags
2. Environment variables
3. `config.json` (`--config path/to/config.json`)
4. Built-in localhost defaults

**Environment variables**


| Variable                     | Meaning                         | Default                 |
| ---------------------------- | ------------------------------- | ----------------------- |
| `OPENSEARCH_PRIMARY_URL`     | PRIMARY URL                     | `http://localhost:9200` |
| `OPENSEARCH_SECONDARY_URL`   | SECONDARY URL                   | `http://localhost:9201` |
| `OPENSEARCH_INDICES_FILE`    | Path to `indices.json`          | `./indices.json`        |
| `OPENSEARCH_PRIMARY_ALIAS`   | Alias on SECONDARY → PRIMARY    | `primary-cluster`       |
| `OPENSEARCH_SECONDARY_ALIAS` | Alias on PRIMARY → SECONDARY    | `secondary-cluster`     |
| `OPENSEARCH_USERNAME`        | Basic auth user                 | (empty)                 |
| `OPENSEARCH_PASSWORD`        | Basic auth password             | (empty)                 |
| `OPENSEARCH_RETRY_MAX`       | Max `_start` attempts per index | `3`                     |
| `OPENSEARCH_RETRY_DELAY`     | Base backoff seconds            | `2.0`                   |


**Example** `config.json`

```json
{
  "primary_url": "https://search-primary.example.com",
  "secondary_url": "https://search-secondary.example.com",
  "indices_file": "./indices.json",
  "primary_alias_on_secondary": "primary-cluster",
  "secondary_alias_on_primary": "secondary-cluster",
  "username": "",
  "password": "",
  "verify_ssl": true,
  "retry_max_attempts": 3,
  "retry_base_delay": 2.0
}
```

---



## 7. Operations in detail

Always run **status first** so you know which cluster is currently the follower.

```bash
python3 dr-switchover.py --operation status
```

What you should see in the **normal** state:

- SECONDARY: static indices `status=SYNCING`, write blocks present (CCR read-only).
- PRIMARY: those same indices have no replication (they are the leader).
- Auto-follow stats on SECONDARY list your rules (`products=products*`, etc.).

---



### 7.1 Status

Read-only. Does not change either cluster.

Shows:

- Cluster health for PRIMARY and SECONDARY
- CCR status per **static** index on both sides (`SYNCING`, `BOOTSTRAPPING`, `PAUSED`, or HTTP error)
- Write / read-only blocks per static index
- Auto-follow stats (success/fail counts, failed index names) when rules exist

Auto-follow-matched indices (`products-1`, `metrics-idx-03`, …) do not appear in the per-index table. They appear under auto-follow stats and in the failover/failback teardown lists.

---



### 7.2 Failover — activate the DR site

**When to use:** PRIMARY is unavailable, or you are running a planned DR test.

**Prerequisite:** You are in the **normal** state (SECONDARY is following PRIMARY). Do **not** run failover twice in a row without failback, unless you understand you are reversing direction again.

```bash
python3 dr-switchover.py --operation failover
```


| Step | What happens                                                                                                                                                                                                                          | Confirmation                           |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| 1    | Stop CCR + remove write blocks on SECONDARY for **static** indices only                                                                                                                                                               | `y/N` if there are static indices      |
| 2    | Discover all indices matching each auto-follow pattern on SECONDARY, stop CCR, remove write blocks, delete the rules                                                                                                                  | `y/N` if there are auto-follow rules   |
| —    | SECONDARY is now writable. DR testing can begin.                                                                                                                                                                                      | —                                      |
| 3    | On PRIMARY: if any static index is still a CCR follower, stop + unblock it. Discover auto-follow matches. Ask you to type `DELETE`. Delete those indices. Start explicit CCR for **static** indices only (PRIMARY follows SECONDARY). | Type `DELETE` (uppercase)              |
| 4    | Create auto-follow rules on PRIMARY (now the follower). If a rule already exists with the same pattern, it is skipped. If the pattern differs, you are asked whether to replace it.                                                   | Replace prompt only if pattern changed |


**Destructive Step 3** permanently deletes the listed indices on the **new follower** (PRIMARY during failover). That is required: CCR cannot start if the follower index already exists. Confirm the **leader** (SECONDARY after Step 1) has the data you care about before typing `DELETE`.

---



### 7.3 Failback — restore the normal state

**When to use:** After a successful failover and DR test. Applications should be ready to write to PRIMARY again.

```bash
python3 dr-switchover.py --operation failback
```

Same four-step shape, opposite direction:


| Step | Cluster acted on                                                                 |
| ---- | -------------------------------------------------------------------------------- |
| 1    | Stop static CCR on PRIMARY, make those indices R/W                               |
| 2    | Tear down auto-follow on PRIMARY (stop matching followers, delete rules)         |
| 3    | Delete listed indices on SECONDARY, start static CCR (SECONDARY follows PRIMARY) |
| 4    | Recreate auto-follow rules on SECONDARY                                          |


---



## 8. What you will be asked to confirm


| Prompt             | Typical text                                                             | How to proceed                               |
| ------------------ | ------------------------------------------------------------------------ | -------------------------------------------- |
| Step 1             | `Stop replication for N index(es) on SECONDARY/PRIMARY … [y/N]`          | Type `y`                                     |
| Step 2             | `Delete N auto-follow rule(s) from SECONDARY/PRIMARY … [y/N]`            | Type `y`                                     |
| Step 3             | `Type DELETE (uppercase) to confirm`                                     | Type `DELETE`                                |
| Rule replace       | `Replace existing auto-follow rule 'products' (pattern: 'old' → 'new')?` | `y` only if you intend to change the pattern |
| Write-block errors | `Proceed anyway?`                                                        | Only if you reviewed the errors              |


`--non-interactive` auto-confirms every prompt (including `DELETE`). Use only in CI or when you are certain.

`--dry-run` skips confirmations and does not mutate the cluster. GET calls (status) still run.

```bash
python3 dr-switchover.py --dry-run --operation failover --non-interactive
```

---



## 9. Recommended first-time practice (local)

Do this on Docker before you touch a real domain.

1. Start clusters (`docker compose up -d`) and wait for init.
2. Install Python deps (section 5).
3. Edit `indices.json` so every `"autofollow": true` entry has `autofollow_pattern` (section 4).
4. Preview:
  ```bash
   python3 dr-switchover.py --dry-run --operation failover --non-interactive
  ```
5. Check live state:
  ```bash
   python3 dr-switchover.py --operation status
  ```
6. Failover, then status again. SECONDARY should be the leader. PRIMARY should show `SYNCING` for static indices. Auto-follow rules should appear on PRIMARY.
7. Optionally create extra matching indices on the **new leader** to watch auto-follow pick them up:
  ```bash
   curl -X PUT http://localhost:9201/products-1
   curl -X PUT http://localhost:9201/products-2
   # wait a few seconds, then:
   curl http://localhost:9200/_cat/indices/products*?v
  ```
8. Failback, then status again. You should be back to PRIMARY → SECONDARY.
9. If something looks wrong, do **not** guess a second failover. Run status, read the errors, then decide.

---



## 10. Adding, changing, or removing indices

Edit `indices.json`. The script reads it on every run. You do not restart Docker for script changes.

### Add a static index

1. Add a Shape A entry.
2. Create the index on the **current leader** (PRIMARY in the normal state).
3. Start CCR once on the follower (init does this for named indices on first boot; on an existing cluster start it yourself or use `[find-missing-replication-indices.py](find-missing-replication-indices.py)`).



### Add an auto-follow family

1. Add a Shape B or C entry with `autofollow_pattern`.
2. Create matching indices on the **current leader**.
3. Run failover or failback (or create the rule yourself). After the next switch, the script creates the rule on the new follower.



### Remove an index from management

Delete its entry from `indices.json`. The script will no longer stop/start it. Existing CCR or auto-follow rules on the cluster are **not** removed until you run a switch (or delete them manually).

---

## 11. Troubleshooting


| What you see                                                     | Likely cause                                                              | What to do                                                                                             |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `autofollow: true requires 'autofollow_pattern'`                 | Missing pattern                                                           | Add `autofollow_pattern` or set `autofollow` to `false`                                                |
| `indices.json has no named indices and no autofollow rules`      | Empty or all-invalid file                                                 | Fix the file                                                                                           |
| Connection / timeout errors                                      | Cluster down or wrong URL                                                 | Check Docker / VPC / `--primary-url`                                                                   |
| Alias errors when starting replication                           | Connection alias missing                                                  | Create `primary-cluster` / `secondary-cluster` (section 5)                                             |
| HTTP 403 `FORBIDDEN/1000` on delete                              | Index is still a CCR follower                                             | The script should stop + unblock and retry. If it still fails, stop replication manually then delete   |
| Failover Step 3 lists only static indices, not `products-*`      | Those indices do not exist on the target cluster, or the pattern is wrong | Confirm names with `_cat/indices` and the pattern in `indices.json`                                    |
| Auto-follow rule already exists                                  | Same name already registered                                              | Same pattern → skipped. Different pattern → you are prompted to replace                                |
| Status shows `SYNCING` on the cluster you thought was the leader | You are already in the DR (or reversed) state                             | Run the **other** operation (failback vs failover). Do not assume “failover” is always next            |
| Second failover without failback                                 | PRIMARY is already a follower                                             | The script should detect CCR and stop it before delete. Prefer failback to return to normal            |
| Init started CCR for an auto-follow `name`                       | `setup.sh` starts CCR for every `"name"` in JSON                          | That is local-seed behavior. The switchover script still treats `autofollow: true` as auto-follow only |


Manual stop + unblock (last resort):

```bash
curl -X POST http://localhost:9201/_plugins/_replication/orders/_stop -H 'Content-Type: application/json' -d '{}'
curl -X PUT  http://localhost:9201/orders/_settings -H 'Content-Type: application/json' \
  -d '{"index":{"blocks.write":null,"blocks.read_only":null,"blocks.read_only_allow_delete":null}}'
```

---



## 12. Safety rules

1. Run `--operation status` before every failover or failback.
2. Preview with `--dry-run` the first time you use a new `indices.json` or new endpoints.
3. Step 3 **deletes** indices on the new follower. Type `DELETE` only after you confirm the other cluster is the good copy.
4. Do not use `--non-interactive` against production until you have practiced locally.
5. After failover, send application writes to **SECONDARY**. After failback, send them back to **PRIMARY**.
6. Auto-follow only stops **new** matching indices when you delete a rule. This script also stops existing matching followers so they become writable. Do not skip Step 2.

---



## 13. Quick command cheat sheet

```bash
# Setup (local)
docker compose up -d
docker logs -f opensearch-init
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Inspect
python3 dr-switchover.py --operation status
python3 dr-switchover.py --dry-run --operation failover --non-interactive

# Switch
python3 dr-switchover.py --operation failover
python3 dr-switchover.py --operation failback

# Automation
python3 dr-switchover.py --operation failover --non-interactive

# AWS-style
python3 dr-switchover.py --config ./config.json --operation status
```

