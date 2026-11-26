# sciget-core local API, version 1

`sciget serve` runs a small HTTP service for the desktop window and the web UI.
The service and the CLI use the same Python API (`sciget_core.manager`). The
HTTP layer adds no behaviour of its own.

## Transport and authentication

- The service listens on `127.0.0.1` only, on the port given by `--port`. The
  default is 0, which picks a free port.
- At startup it creates a random API token. It writes `{"url": ..., "token": ...}`
  to `<user_state_dir>/sciget/serve.json` (mode 0600) and prints the UI address
  as `http://127.0.0.1:<port>/#token=<token>`. The token goes in the URL
  fragment, so the browser never sends it in a request line and it never shows
  up in a log.
- The UI reads the token from `location.hash`, keeps it in `sessionStorage` and
  removes it from the address bar. It sends `Authorization: Bearer <token>` on
  every `/api/` request.
- The server rejects:
  - a request without a valid token (401);
  - a `Host` header other than `127.0.0.1:<port>` or `localhost:<port>` (421,
    against DNS rebinding);
  - a request whose `Origin` header is present and is not the service's own
    origin (403).
- Static UI files under `/` need no token.
- Every response sends `Cache-Control: no-store` and
  `X-Content-Type-Options: nosniff`. The UI pages also send
  `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'`, so the
  UI can't load anything from a third party.

## Errors

A failed call returns a JSON body `{"error": "<code>", "message": "<text a person can read>"}`.
The `<code>` values are `bad_request` (400, and also 403/421 from the Origin and Host
checks, 413, 415), `not_found` (404), `unauthorized` (401), `engine_unavailable` (503)
and `internal` (500). The UI should show `message`; codes are for programs.

## Objects

`Domain`
```json
{"name": "neurodesk", "title": "Neurodesk", "description": "...",
 "image": "docker.io/vnmd/neurodesktop", "default_version": "2026-09-23",
 "remote_urls": ["https://play-europe.neurodesk.org/"], "cvmfs_repository": "neurodesk.ardc.edu.au"}
```

`Engine`
```json
{"kind": "podman", "available": true, "rootless": true, "version": "5.6.2"}
```
`kind` is one of `podman`, `docker` or `apptainer`.

`Session`
```json
{"id": "k3v9q2", "domain": "neurodesk", "engine": "podman", "version": "2026-09-23",
 "state": "starting", "created": "2026-11-26T10:12:00Z",
 "port": 41733, "data_dir": "/home/me/project", "cvmfs": "host", "gpu": false,
 "url": "http://127.0.0.1:41733/lab?token=...", "error": null}
```
- `state` is one of `pulling`, `starting`, `ready`, `stopped` or `error`.
- `url` opens JupyterLab and includes that session's own Jupyter token. Only an
  authenticated API call returns it.
- `cvmfs` is one of `host`, `container` or `off`. It is the mode actually in use,
  not the one that was asked for.

`Settings`
```json
{"engine": "auto", "cvmfs": "auto", "gpu": false, "storage_dir": "/home/me/neurodesktop-storage",
 "favorites": ["neurodesk"]}
```

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/v1/health` | none | `{"ok": true, "version": "0.1.0"}` |
| GET | `/api/v1/domains` | none | `[Domain]` |
| GET | `/api/v1/engines` | none | `[Engine]` |
| GET | `/api/v1/settings` | none | `Settings` |
| PUT | `/api/v1/settings` | the `Settings` fields to change | `Settings` |
| GET | `/api/v1/sessions` | none | `[Session]` |
| POST | `/api/v1/sessions` | `{"domain", "engine"?, "version"?, "data_dir"?, "cvmfs"?, "gpu"?}` | 202 `Session` |
| GET | `/api/v1/sessions/{id}` | none | `Session` |
| DELETE | `/api/v1/sessions/{id}` | none | 200 `Session`, now in state `stopped` |
| GET | `/api/v1/sessions/{id}/logs?tail=N` | none | `{"lines": ["..."]}` |

Notes:
- `POST /sessions` returns as soon as the session record exists. The UI then
  polls `GET /sessions/{id}` about once a second until `state` is `ready` or
  `error`.
- `data_dir` must be an existing directory. It is mounted at `/data`. Leave it out
  for no `/data` mount. It is not the storage folder: `Settings.storage_dir` is
  always mounted at the domain's storage path (e.g. `/neurodesktop-storage`).
- `engine` and `cvmfs` accept `"auto"` as well as the specific values.
- Any field left out of a `POST /sessions` body takes its value from `Settings`.
