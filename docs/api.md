# API

## Implemented Routes

- `POST /endpoints`
- `GET /endpoints`
- `GET /endpoints/{endpoint_id}/health`
- `POST /environments`
- `GET /environments`
- `GET /environments/{environment_id}`
- `POST /environments/{environment_id}/reconcile`
- `POST /workspaces`
- `GET /workspaces`
- `POST /workspaces/{workspace_id}/roots`
- `POST /workspaces/{workspace_id}/replicas`
- `POST /workspaces/{workspace_id}/bindings`
- `POST /workspaces/{workspace_id}/revisions/{replica_id}`
- `POST /workspaces/{workspace_id}/sync/{binding_id}`
- `POST /tasks`
- `GET /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/logs`
- `POST /tasks/{task_id}/cancel`
- `POST /sessions`
- `GET /sessions`
- `GET /sessions/{session_id}`
- `POST /sessions/{session_id}/write`
- `POST /sessions/{session_id}/terminate`
- `POST /interactions/requests`
- `POST /interactions/requests/{request_id}/submit`
- `POST /interactions/leases`
- `POST /artifacts`
- `GET /artifacts`
- `POST /artifacts/{artifact_id}/download`

## Not Yet Implemented

- WebSocket event streaming
- terminal streaming API
- remote forwarding endpoints
