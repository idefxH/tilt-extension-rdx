# Changelog

## Unreleased

### Added

- **BCI builder: Paketo build-time knobs surfaced as `rdx_app()` arguments.**
  The `builder='bci'` path (which ships Paketo buildpacks) now exposes the
  Paketo features the heroku builder can't, each as a dedicated argument
  that maps to a documented `BP_*` / `BPE_*` `pack build --env` flag:
  - `live_reload=True` → `BP_LIVE_RELOAD_ENABLED=true`. Paketo wraps the
    buildpack's own `web` process in watchexec, so the extension selects
    `--default-process web` and excludes the Procfile from the build
    context (a `web:` Procfile entry runs after npm-start in the BCI
    builder and would overwrite the watchexec-wrapped `web`, silently
    breaking reload). This is the Paketo-native replacement for the heroku
    nodemon `dev:` hack — a Tilt file sync restarts just the app process.
    Supported for nodejs/python/java (the BCI builder bundles the
    `watchexec` buildpack for those); ignored with a warning for go (its
    `go-build` buildpack has no watchexec support, and Go isn't in the run
    image). Opt-in, not the default: watchexec uses inotify, which can miss
    events on some containerd/overlayfs setups, so the verified nodemon
    path stays the default.
  - `node_version=` → `BP_NODE_VERSION` (nodejs). Paketo also auto-reads
    `.node-version` / `.nvmrc` / `package.json#engines.node`.
  - `bp_log_level=` → `BP_LOG_LEVEL` (e.g. `'DEBUG'`).
  - `image_labels={...}` → `BP_IMAGE_LABELS` (OCI labels). The
    space-delimited value is now shell-quoted in the emitted `pack`
    command (see Fixed) so a multi-label value doesn't split into stray
    positional args.
  - `runtime_cert_binding=False` → `BP_ENABLE_RUNTIME_CERT_BINDING=false`
    (runtime cert binding is on by default in Paketo, so this is opt-OUT).
  - `debug=True` / `debug_port=` → `BPE_DEFAULT_BPL_DEBUG_ENABLED` /
    `_PORT`, baking a launch-time debug default via the
    environment-variables buildpack (honoured by the JVM).

  All knobs are no-ops on the heroku builder, so setting one with
  `builder='heroku'` prints a single warning and is ignored rather than
  baking a dead env layer. Dedicated arguments win over the raw
  `additional_env={...}` escape hatch on a key clash. Monorepo subdirs
  need no new flag — the existing `build_path=` already scopes the build
  context via `pack build --path`. Guarded by `test_bci_paketo_env.py`
  (which extracts and execs the real Tiltfile helpers so the tests can't
  drift from the code) and verified end-to-end with
  `tilt alpha tiltfile-result` against the example app.

### Fixed

- **BCI builder: `live_reload=True` no longer hard-fails the build.** The
  first cut selected `--default-process reload`, but there is no `reload`
  process: with `BP_LIVE_RELOAD_ENABLED=true` the Paketo language buildpack
  wraps its OWN `web` process in watchexec (`web (default): watchexec
  --restart … -- sh start.sh`), so `pack` failed with `tried to set reload
  to default but it doesn't exist`. Now the extension selects
  `--default-process web` AND excludes the Procfile from the build context
  (via a generated `project.toml` with `exclude = ["Procfile"]`) — the
  BCI builder's procfile buildpack runs after npm-start and a `web:`
  Procfile entry would otherwise overwrite the watchexec-wrapped `web` with
  a plain command, silently disabling reload. Projects that ship their own
  `project.toml` are left untouched (warned, not clobbered).

- **BCI builder: `BP_IMAGE_LABELS` (and any space-bearing `pack --env`
  value) is now shell-quoted.** `custom_build` runs the `pack` command
  through a shell, so a `--env BP_IMAGE_LABELS=a=1 b=2` token split `b=2`
  off into a stray positional argument to `pack`. Space-bearing values are
  now wrapped in double quotes, matching the existing `--cache` quoting.


- **BCI builder: live_update now reloads the app (was stuck "updating").**
  With `builder='bci'`, editing a source file copied the file into the
  container but the app never restarted — heroku worked fine. Root cause
  (found via `docker inspect` + a real `pack build`): the BCI builder
  builds as `CNB_USER_ID=1001`, but its run image launches the container
  as uid `1002`. The CNB lifecycle exports `/workspace` owned by the
  build user (1001) with `0755` dirs / `0644` files, so the launch user
  (1002) cannot overwrite anything under `/workspace/src`. Tilt's
  live_update sync runs as the pod user, so its copy silently fails with
  `EACCES`, nodemon never sees a change, and the resource hangs in
  "updating". The heroku builder uses one uid for build+launch, so its
  sync works — hence the bug was BCI-only. `rdx_app()` now pins the app
  pod's `runAsUser`/`runAsGroup` to the BCI builder's build uid/gid
  (1001/1000) via the library chart's `podSecurityContext` so the
  container owns the files live_update overwrites. Verified locally:
  running the built image with `--user 1001:1000` makes the in-container
  overwrite succeed and nodemon logs `restarting due to changes...`.
  Override with `RDX_BCI_RUNAS_UID` / `RDX_BCI_RUNAS_GID`, or set
  `RDX_BCI_RUNAS_UID=-1` to disable. Guarded by `test_bci_runas_uid.py`.

### Changed

- **Operator webhook hooks are now catalog-driven.** The CNPG-specific
  webhook-prune and endpoint-ready-gate logic in `rdx_app()` is now a
  generic mechanism keyed off three (optional) `dsl-mappings.yaml` fields
  on any `operator_managed: true` version entry: `webhook_service_name`,
  `webhook_namespace` (default: release namespace), and
  `webhook_app_label` (label-selector value for the prune scan). A small
  `_OPERATOR_DEFAULTS` table in the Tiltfile supplies the same values
  for `cnpg` so older bundles whose catalog doesn't declare these fields
  yet behave identically. The readiness-gate resource is now named
  `<release>-<service_type>-webhook-ready` (was `<release>-cnpg-webhook-ready`
  — unchanged for cnpg). Any future operator-managed chart can opt in
  by adding the three fields to its catalog entry.

### Fixed

- **CNPG: prune stale cluster-scoped webhook configs at tilt up.** `tilt down`
  does not reliably remove the `Mutating/ValidatingWebhookConfiguration`
  objects the cloudnative-pg sub-chart installs (they are cluster-scoped and
  templated off the release name). Configs left behind from a prior scenario
  have `clientConfig.service.namespace` pointing at a now-deleted namespace,
  and the kube-apiserver blocks every subsequent CNPG admission with
  `no endpoints available for service "cnpg-webhook-service"`. When `cnpg`
  is in the service catalog, rdx_app() now scans webhook configs labelled
  `app.kubernetes.io/name=cloudnative-pg`, deletes any whose target
  namespace no longer exists, and lets the fresh helm install re-create
  them clean. Found via rdx-e2e-tests.
- **CNPG: gate the Cluster CR apply on operator webhook-service endpoints.**
  On a cold cluster the operator pod image pull can run >5s. The Cluster CR
  is in the same helm manifest stream as the operator Deployment, and the
  apiserver tries to validate the CR via the webhook the moment kubectl
  apply lands. With no operator pod yet, `tilt ci` failed with
  `no endpoints available for service "cnpg-webhook-service"`. A new
  `<release>-cnpg-webhook-ready` local_resource polls the operator's
  webhook Service Endpoints for up to 180s (2s × 90), and the Cluster CR's
  k8s_resource resource_deps on it. Found via rdx-e2e-tests.
- **Disable Tilt's secret-value scrubber by default.** The rdx library-chart
  writes Service-Binding-Spec-compliant Secrets whose stringData contains
  the chart `type` field (`postgresql`, `prometheus`, `grafana`, `dex`, …) —
  required by the SBS. Tilt's built-in scrubber harvests every Secret value
  it deploys and substitutes the literal strings everywhere in its UI, so a
  workload named `app-prometheus-server` displayed as
  `app-[redacted secret app-mon-binding:type]-server` — unintelligible to
  the dev, mismatch with `kubectl get`. The k8s_resource registrations
  themselves were always correct; only the display layer was mangled.
  Now off by default; opt back in with `RDX_ENABLE_TILT_SCRUB=1`
  (e.g. for screen-shared demos).

## [0.5.0] - 2026-05-10

_helm-rdx plugin integration (rdx-cli v0.2.0+)._

### Added

- `helm_rdx_chart(name, chart_path, stage, values, namespace, set)` — thin
  chart-rendering primitive that uses `helm rdx template` when the helm-rdx
  plugin is installed (one shell call: DSL projection + `helm template`,
  no `values.generated.yaml` on disk), falls back to `rdx render` + Tilt's
  `helm()` builtin otherwise. Plugin detection via `helm plugin list`.
  Composes with `k8s_resource()` for port-forwards; `rdx_app()` is
  unchanged for the batteries-included path.

## [0.4.0] - 2026-05-05

_Multi-instance same-chart-type support._

### Added

- **Multi-instance:** `filter_enabled_deps` expands Chart.yaml with aliased entries for multi-instance types
- Add alias-aware `workload_name_for()` — per-binding k8s_resource registration
- Add port collision avoidance for multi-instance same-type services
- Add `enabled_charts()` returns aliased names for multi-instance types

### Fixed

- Fix `_chart_enabled` to check aliased blocks (`grafana-dashboards.enabled`)
- Remove debug print from workload registration loop

### Changed

- Replace CLI references: `rdx add-service` → `rdx service add`, `rdx add-datasource` → `rdx service wire`

## [0.3.4] - 2026-05-04

### Added

- Read service ports from dsl-mappings.yaml (zero-config, data-driven)
- Remap privileged ports (<1024) to `port+10000` for local port-forwards

## [0.3.3] - 2026-05-04

### Fixed

- Add `heroku/procfile` to Java buildpack defaults (fixes "no default process" crash)

[0.5.0]: https://github.com/idefxH/tilt-extension-rdx/releases/tag/v0.5.0
[0.4.0]: https://github.com/idefxH/tilt-extension-rdx/releases/tag/v0.4.0
[0.3.4]: https://github.com/idefxH/tilt-extension-rdx/releases/tag/v0.3.4
[0.3.3]: https://github.com/idefxH/tilt-extension-rdx/releases/tag/v0.3.3
