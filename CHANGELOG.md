# Changelog

## Unreleased

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
