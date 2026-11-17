# Teams

One folder per team. The CloudBees pipeline's `TEAM` parameter selects the folder.

| File | Used for |
|---|---|
| `<team>/indices.json` | Indices and auto-follow rules that the DR switchover manages for this team. Same schema as the root `indices.json`. |

In the Docker POC, each team also gets its own primary + secondary cluster pair, as compose project `ccr-<team>`. The pair is created on that team's first pipeline run and then kept.

> The three `indices.json` files start as copies of the root `indices.json`. Replace them with each team's real indices.

## Adding a team

1. Create `teams/<team>/indices.json`.
2. Add `<team>` to the `TEAM` choice list in `ci/Jenkinsfile`.

Phase 3 (AWS) adds `teams/<team>/config.json` with the team's domain endpoints and Secrets Manager ARN.
