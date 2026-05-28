#!/bin/sh
# Runs once after both clusters are healthy.
# 1. Creates mock indices + data in primary
# 2. Registers primary as remote cluster on secondary   (alias: primary-cluster)
# 3. Registers secondary as remote cluster on primary   (alias: secondary-cluster)
# 4. Starts CCR for every index listed in /indices.json (secondary follows primary)
#
# Both remote registrations are done up-front so that the DR switchover script
# can start replication in either direction without needing to register anything.

set -e

PRIMARY="http://opensearch-primary:9200"
SECONDARY="http://opensearch-secondary:9200"
PRIMARY_ALIAS="primary-cluster"    # alias on SECONDARY pointing to PRIMARY
SECONDARY_ALIAS="secondary-cluster" # alias on PRIMARY pointing to SECONDARY
INDICES_FILE="/indices.json"

log() { echo "[setup] $*"; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

wait_for_cluster() {
  local url="$1"
  local name="$2"
  log "Waiting for $name to be green/yellow..."
  for i in $(seq 1 40); do
    status=$(curl -s "$url/_cluster/health" | grep -o '"status":"[^"]*"' | cut -d'"' -f4 || true)
    if [ "$status" = "green" ] || [ "$status" = "yellow" ]; then
      log "$name is $status"
      return 0
    fi
    log "  attempt $i: status='$status', retrying in 5s..."
    sleep 5
  done
  log "ERROR: $name did not become healthy in time"
  exit 1
}

# Parse index names from indices.json using only sh builtins + sed/grep
get_index_names() {
  # Returns one index name per line
  grep '"name"' "$INDICES_FILE" | sed 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/'
}

# ---------------------------------------------------------------------------
# Step 1 – Wait for both clusters
# ---------------------------------------------------------------------------
wait_for_cluster "$PRIMARY"   "primary"
wait_for_cluster "$SECONDARY" "secondary"

# ---------------------------------------------------------------------------
# Step 2 – Create indices + bulk mock data in primary
# ---------------------------------------------------------------------------
log "=== Creating indices and seeding mock data in primary ==="

create_index() {
  local index="$1"
  local shards="$2"
  local replicas="$3"

  log "Creating index '$index' (shards=$shards, replicas=$replicas)..."
  curl -s -o /dev/null -w "  HTTP %{http_code}\n" \
    -X PUT "$PRIMARY/$index" \
    -H "Content-Type: application/json" \
    -d "{
      \"settings\": {
        \"number_of_shards\": $shards,
        \"number_of_replicas\": $replicas
      }
    }"
}

# Seed data generators -------------------------------------------------

seed_products() {
  log "Seeding 'products'..."
  curl -s -o /dev/null -X POST "$PRIMARY/products/_bulk" \
    -H "Content-Type: application/x-ndjson" \
    -d '
{"index":{"_id":"1"}}
{"name":"Wireless Headphones","category":"Electronics","price":79.99,"stock":150,"sku":"ELEC-WH-001"}
{"index":{"_id":"2"}}
{"name":"Running Shoes","category":"Sportswear","price":120.00,"stock":85,"sku":"SPORT-RS-002"}
{"index":{"_id":"3"}}
{"name":"Coffee Maker","category":"Appliances","price":49.99,"stock":60,"sku":"APPL-CM-003"}
{"index":{"_id":"4"}}
{"name":"Yoga Mat","category":"Fitness","price":25.00,"stock":200,"sku":"FIT-YM-004"}
{"index":{"_id":"5"}}
{"name":"Mechanical Keyboard","category":"Electronics","price":89.99,"stock":45,"sku":"ELEC-MK-005"}
'
}

seed_orders() {
  log "Seeding 'orders'..."
  curl -s -o /dev/null -X POST "$PRIMARY/orders/_bulk" \
    -H "Content-Type: application/x-ndjson" \
    -d '
{"index":{"_id":"ORD-1001"}}
{"order_id":"ORD-1001","user_id":"U001","status":"shipped","total":79.99,"items":[{"sku":"ELEC-WH-001","qty":1}],"created_at":"2026-05-01T10:00:00Z"}
{"index":{"_id":"ORD-1002"}}
{"order_id":"ORD-1002","user_id":"U002","status":"delivered","total":240.00,"items":[{"sku":"SPORT-RS-002","qty":2}],"created_at":"2026-05-03T14:30:00Z"}
{"index":{"_id":"ORD-1003"}}
{"order_id":"ORD-1003","user_id":"U003","status":"pending","total":49.99,"items":[{"sku":"APPL-CM-003","qty":1}],"created_at":"2026-05-25T09:15:00Z"}
{"index":{"_id":"ORD-1004"}}
{"order_id":"ORD-1004","user_id":"U001","status":"processing","total":114.99,"items":[{"sku":"FIT-YM-004","qty":1},{"sku":"ELEC-WH-001","qty":1}],"created_at":"2026-05-26T18:45:00Z"}
{"index":{"_id":"ORD-1005"}}
{"order_id":"ORD-1005","user_id":"U004","status":"delivered","total":89.99,"items":[{"sku":"ELEC-MK-005","qty":1}],"created_at":"2026-05-10T11:00:00Z"}
'
}

seed_users() {
  log "Seeding 'users'..."
  curl -s -o /dev/null -X POST "$PRIMARY/users/_bulk" \
    -H "Content-Type: application/x-ndjson" \
    -d '
{"index":{"_id":"U001"}}
{"user_id":"U001","name":"Alice Johnson","email":"alice@example.com","country":"US","tier":"gold","joined_at":"2024-01-15"}
{"index":{"_id":"U002"}}
{"user_id":"U002","name":"Bob Smith","email":"bob@example.com","country":"UK","tier":"silver","joined_at":"2024-03-22"}
{"index":{"_id":"U003"}}
{"user_id":"U003","name":"Carol White","email":"carol@example.com","country":"AU","tier":"bronze","joined_at":"2025-07-10"}
{"index":{"_id":"U004"}}
{"user_id":"U004","name":"Dan Lee","email":"dan@example.com","country":"SG","tier":"gold","joined_at":"2023-11-01"}
{"index":{"_id":"U005"}}
{"user_id":"U005","name":"Eva Martinez","email":"eva@example.com","country":"ES","tier":"silver","joined_at":"2025-02-28"}
'
}

seed_events() {
  log "Seeding 'events'..."
  curl -s -o /dev/null -X POST "$PRIMARY/events/_bulk" \
    -H "Content-Type: application/x-ndjson" \
    -d '
{"index":{"_id":"EVT-001"}}
{"event_id":"EVT-001","type":"page_view","user_id":"U001","page":"/home","timestamp":"2026-05-27T08:00:00Z","session":"S100"}
{"index":{"_id":"EVT-002"}}
{"event_id":"EVT-002","type":"search","user_id":"U002","query":"wireless headphones","timestamp":"2026-05-27T08:05:00Z","session":"S101"}
{"index":{"_id":"EVT-003"}}
{"event_id":"EVT-003","type":"add_to_cart","user_id":"U002","sku":"ELEC-WH-001","timestamp":"2026-05-27T08:07:00Z","session":"S101"}
{"index":{"_id":"EVT-004"}}
{"event_id":"EVT-004","type":"checkout","user_id":"U002","order_id":"ORD-1006","timestamp":"2026-05-27T08:10:00Z","session":"S101"}
{"index":{"_id":"EVT-005"}}
{"event_id":"EVT-005","type":"login","user_id":"U003","ip":"203.0.113.5","timestamp":"2026-05-27T09:00:00Z","session":"S102"}
'
}

# Create and seed each known index
create_index "products" 1 0
seed_products
create_index "orders" 1 0
seed_orders
create_index "users" 1 0
seed_users
create_index "events" 1 0
seed_events

# Refresh so docs are immediately searchable
log "Refreshing all indices on primary..."
curl -s -o /dev/null -X POST "$PRIMARY/_refresh"

# ---------------------------------------------------------------------------
# Step 3 – Register remote clusters in both directions
# ---------------------------------------------------------------------------

# 3a: Register PRIMARY as a remote on SECONDARY
#     Used by the DR script during FAILBACK (secondary follows primary — normal state)
log "=== Registering primary as remote cluster on secondary (alias: $PRIMARY_ALIAS) ==="

curl -s -o /dev/null -w "  HTTP %{http_code}\n" \
  -X PUT "$SECONDARY/_cluster/settings" \
  -H "Content-Type: application/json" \
  -d "{
    \"persistent\": {
      \"cluster\": {
        \"remote\": {
          \"$PRIMARY_ALIAS\": {
            \"seeds\": [\"opensearch-primary:9300\"]
          }
        }
      }
    }
  }"

# 3b: Register SECONDARY as a remote on PRIMARY
#     Used by the DR script during FAILOVER (primary follows secondary — DR active state)
log "=== Registering secondary as remote cluster on primary (alias: $SECONDARY_ALIAS) ==="

curl -s -o /dev/null -w "  HTTP %{http_code}\n" \
  -X PUT "$PRIMARY/_cluster/settings" \
  -H "Content-Type: application/json" \
  -d "{
    \"persistent\": {
      \"cluster\": {
        \"remote\": {
          \"$SECONDARY_ALIAS\": {
            \"seeds\": [\"opensearch-secondary:9300\"]
          }
        }
      }
    }
  }"

log "Waiting 5s for remote cluster handshakes..."
sleep 5

# Verify both connections
log "Verifying remote cluster connections..."
log "  Secondary sees:"
curl -s "$SECONDARY/_remote/info"
echo ""
log "  Primary sees:"
curl -s "$PRIMARY/_remote/info"
echo ""

# ---------------------------------------------------------------------------
# Step 4 – Start replication for each index
# ---------------------------------------------------------------------------
log "=== Starting cross-cluster replication on secondary ==="

INDEX_NAMES=$(get_index_names)

for index in $INDEX_NAMES; do
  log "Starting replication for index '$index'..."
  http_code=$(curl -s -o /tmp/repl_response.txt -w "%{http_code}" \
    -X PUT "$SECONDARY/_plugins/_replication/${index}/_start" \
    -H "Content-Type: application/json" \
    -d "{
      \"leader_alias\": \"$PRIMARY_ALIAS\",
      \"leader_index\": \"$index\",
      \"use_roles\": {
        \"leader_cluster_role\": \"all_access\",
        \"follower_cluster_role\": \"all_access\"
      }
    }")
  response=$(cat /tmp/repl_response.txt)
  log "  index=$index  http=$http_code  response=$response"
done

# ---------------------------------------------------------------------------
# Step 5 – Summary
# ---------------------------------------------------------------------------
log ""
log "=========================================="
log "  Setup complete!"
log "=========================================="
log ""
log "  Primary cluster:   $PRIMARY"
log "  Secondary cluster: $SECONDARY"
log "  Replicated indices:"
for index in $INDEX_NAMES; do
  log "    - $index"
done
log ""
log "  Remote cluster aliases:"
log "    secondary knows primary as : $PRIMARY_ALIAS"
log "    primary knows secondary as : $SECONDARY_ALIAS"
log ""
log "  Check replication status:"
log "    curl http://localhost:9201/_plugins/_replication/<index>/_status"
log "=========================================="
