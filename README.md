# tilt-extension-rdx

Tilt extension for RDX (Rancher Developer eXperience).

Wraps Cloud Native Buildpacks (via `pack`), Helm install, and service
port-forwarding into a single declarative call (`rdx_app(...)`).

## Use

```python
v1alpha1.extension_repo(
    name='rdx',
    url='https://github.com/idefxH/tilt-extension-rdx',
)
v1alpha1.extension(
    name='rdx',
    repo_name='rdx',
    repo_path='rdx',
)
load('ext://rdx', 'rdx_app')

rdx_app(
    name='my-app',
    language='nodejs',
)
```

### Monorepo support

Use `build_path` to point at a subdirectory when the source lives in a monorepo:

```python
rdx_app(
    name='my-app',
    language='go',
    build_path='services/my-app',
)
```

### Multi-container workloads

When `rdx-library.workloads[]` is present in `chart/values.yaml` (bundle v0.11+),
the extension builds each workload whose `image.tag == 'dev'`, registers a
separate `k8s_resource` per workload, and discovers ports per-workload. The
legacy single-workload path remains the default when `workloads[]` is absent.

That replaces ~30 lines of `docker_build` / `helm` / `k8s_resource` boilerplate
with one call. Port is auto-discovered from `rdx-library.port` in values.yaml
(default 8080). **Service port-forwards are auto-discovered from `chart/values.yaml`** — every entry under `rdx-library.services[]` with `provisioning: deploy` AND a matching `<chart>.enabled: true` gets a port-forward registered. The Tiltfile stays in lockstep with `values.yaml` automatically; you don't list services in two places.

Entries with `provisioning: connect`, `shared`, or `external` are skipped (no in-cluster workload to forward — they bind to a pre-existing instance via the binding secret). Their operator sub-charts (e.g. `cloudnative-pg` for a connect-mode postgresql) are also dropped from `Chart.yaml` so `helm dep update` doesn't pull them. Entries whose sub-chart is not enabled are also skipped (no workload deployed).

The pre-DSL legacy auto-discovery (`<chart>.enabled` without `services[]`) was dropped in v0.2.0 alongside bundle v0.10+. If you're on a bundle ≤ v0.9, either upgrade the bundle or pin the extension to its 0.1.x line.

Pass `services={'<binding>': '<type>'}` only when you want to register a service the chart doesn't deploy itself, or narrow the auto-discovered set to a subset. Tilt fetches and caches the extension repo automatically; restart Tilt to pick up updates.

## Prerequisites

- Tilt 0.33+
- `pack` CLI in `$PATH` (https://buildpacks.io)
- `helm` CLI in `$PATH`
- A running Kubernetes cluster (Rancher Desktop, k3d, kind, etc.)

## Repository layout

```
tilt-extension-rdx/
├── README.md
├── LICENSE
├── rdx/
│   └── Tiltfile          # the extension itself (loaded as ext://rdx)
└── examples/
    └── nodejs-hello/     # minimal smoke-test consuming the extension
```

The `rdx/` directory name follows the Tilt convention where each
extension in a repo lives in a subdirectory named after itself.

## Supported languages

| `language` | Default buildpack | Live update |
|---|---|---|
| `nodejs` | `paketo-buildpacks/nodejs` | Sync `./src`, re-run `npm install` on `package.json` change |
| `python` | `paketo-buildpacks/python` | Sync `./src`, re-run `pip install` on requirement-file changes |
| `java` | `paketo-buildpacks/java` | Full rebuild on any change |
| `go` | `paketo-buildpacks/go` | Full rebuild on any change |

## Builders: heroku (default) vs BCI / Paketo

`rdx_app(builder=...)` selects the Cloud Native Buildpack builder:

- `builder='heroku'` (default) — `heroku/builder:24` with `heroku/*`
  buildpacks. Multi-arch, ~400 MB, the safe default.
- `builder='bci'` — the SUSE BCI 16.0 builder
  (`ghcr.io/idefxh/builder-bci-base`), which ships **Paketo** buildpacks.
  Multi-arch (native arm64). Lives in a private registry, so run
  `docker login ghcr.io` once (a preflight warns if it can't see ghcr
  credentials; silence with `RDX_SKIP_GHCR_CHECK=1`).

### Paketo build knobs (BCI only)

These arguments map to documented Paketo `BP_*` / `BPE_*` build-time
variables. They only affect `builder='bci'`; setting any of them with the
heroku builder prints one warning and is ignored (the heroku path already
live-reloads via the Procfile `dev:` nodemon process).

```python
rdx_app(
    name='my-app',
    language='nodejs',
    builder='bci',
    live_reload=True,            # BP_LIVE_RELOAD_ENABLED — watchexec reload
    node_version='22',           # BP_NODE_VERSION (nodejs)
    bp_log_level='DEBUG',        # BP_LOG_LEVEL — verbose buildpack logs
    image_labels={'team': 'platform'},  # BP_IMAGE_LABELS (OCI labels)
)
```

| Argument | Paketo var | Notes |
|---|---|---|
| `live_reload=True` | `BP_LIVE_RELOAD_ENABLED=true` | Builds a watchexec-wrapped `reload` process and runs it as the container default, so a Tilt file sync restarts just the app process — the Paketo-native replacement for the heroku nodemon `dev:` hack. nodejs/python/java only (go's buildpack contributes no reload process; ignored with a warning). watchexec uses inotify by default, which can miss events on some containerd/overlayfs setups — fall back to `live_reload=False` (nodemon) if reloads don't fire. |
| `node_version='22'` | `BP_NODE_VERSION` | nodejs only. Paketo also auto-reads `.node-version`, `.nvmrc`, and `package.json#engines.node` — prefer committing one of those. |
| `bp_log_level='DEBUG'` | `BP_LOG_LEVEL` | First thing to reach for when a BCI build misbehaves. |
| `image_labels={...}` | `BP_IMAGE_LABELS` | OCI labels (space-delimited `key=value`, quoted for you). |
| `runtime_cert_binding=False` | `BP_ENABLE_RUNTIME_CERT_BINDING=false` | Runtime cert binding is **on by default** in Paketo (a `ca-certificates` service binding is read at launch); pass `False` to disable. |
| `debug=True`, `debug_port=5005` | `BPE_DEFAULT_BPL_DEBUG_ENABLED` / `_PORT` | Bakes a launch-time debug default via the environment-variables buildpack (honoured by the JVM). For Node, set `additional_env={'BPE_DEFAULT_NODE_OPTIONS': '--inspect=0.0.0.0:9229'}`. This configures the image only — forward the port yourself. |

For any Paketo knob without a dedicated argument (e.g. Go `BP_GO_TARGETS` /
`BP_GO_BUILD_LDFLAGS`, Java `BP_JVM_VERSION` / `BP_MAVEN_BUILD_ARGUMENTS`,
Python `BP_CPYTHON_VERSION`), use the raw `additional_env={...}` escape
hatch. Dedicated arguments win over `additional_env` on a key clash.

Monorepos need no Paketo-specific flag: `build_path='<subdir>'` already
scopes the build context (`pack build --path <subdir>`) for every builder.

## Brownfield support

For existing apps with a pre-built container image (no pack build):

```python
rdx_app(
    name='my-app',
    chart_path='deploy',
    image_ref='registry.corp.com/my-app:v2.3.1',
)
```

For existing Helm chart projects (no image management at all):

```python
rdx_app(
    name='my-app',
    chart_path='chart',
    helm_only=True,
)
```

When `image_ref` is set, `language` is not needed. When `helm_only` is
True, both `language` and `image_ref` are ignored.

## `helm_rdx_chart()` — helm-rdx plugin integration (rdx-cli v0.2.0+)

Thin chart-rendering primitive that uses the `helm rdx template` plugin
when installed (one shell call: DSL projection + `helm template` with no
intermediate `values.generated.yaml` on disk), and falls back to
`rdx render` + Tilt's `helm()` builtin when not.

```python
load('ext://rdx', 'helm_rdx_chart')

helm_rdx_chart(
    name='my-app',
    chart_path='deploy',
    stage='dev',
)
k8s_resource('my-app', port_forwards='8080:8080')
```

Unlike `rdx_app(...)`, this function does NOT do pack build, helm dep
update, namespace creation, port-forwarding, or pull-secret mirror —
it's the chart-rendering step only. Compose with `k8s_resource()` for
port-forwards. For the batteries-included experience, stick with
`rdx_app(...)`.

Plugin detection: `helm plugin list | grep ^rdx`. Missing plugin →
fallback path with a one-line warning. Missing both plugin and `rdx`
CLI → louder warning, chart still renders without DSL projection.

## Conventional service ports

For each service auto-discovered from `chart/values.yaml` (or passed
explicitly via `services={...}`), the extension forwards the canonical
local port:

| Service type | Local port |
|---|---|
| `postgresql` | 5432 |
| `redis` | 6379 |
| `mysql` | 3306 |
| `mongodb` | 27017 |
| `kafka` | 9092 |
| `rabbitmq` | 5672 |
| `nats` | 4222 |

## Environment variables

| Var | Effect |
|---|---|
| `RDX_DEFAULT_REGISTRY` | Calls `default_registry(...)` at extension load. Use for CI / local-mirror / air-gapped setups (e.g. `localhost:5000` for the e2e kind-mirror). |
| `RDX_SKIP_PULLSECRET_MIRROR=1` | Skip the `default → <ns>` mirror of `Secret/application-collection`. Set when an operator (kubernetes-reflector, External Secrets) distributes the pull secret instead. |
| `RDX_SKIP_GHCR_CHECK=1` | Skip the `builder='bci'` preflight that warns when no `ghcr.io` entry is found in `~/.docker/config.json` (credential helpers store auth elsewhere). |
| `RDX_BCI_RUNAS_UID` / `RDX_BCI_RUNAS_GID` | Override the `builder='bci'` live-update `runAsUser`/`runAsGroup` (defaults 1001/1000, the builder's CNB build uid/gid). Set `RDX_BCI_RUNAS_UID=-1` to disable the override. |

## RDX-SUSE buildpacks (future)

When the RDX-SUSE buildpacks land, override the builder:

```python
rdx_app(..., builder_image='registry.rdx.suse.com/builder:latest')
```

## Where image-level gates fit (forward-looking)

`rdx_app(...)` is the right hook for **image-level gates** — checks
that run at `pack build` time, before Tilt deploys anything to the
cluster. These are Layer 1 of RDX's four-layer defense model. The
canonical reference is [`rdx-docs/concepts/gates.md`](https://github.com/idefxH/rdx-docs/blob/main/concepts/gates.md);
the anchor in the rdx CLI spec is the `BEHAVIOR: promote` NOTES in
[`rdx-cli/rdx.md`](https://github.com/idefxH/rdx-cli/blob/main/rdx.md)
under "Layered-defense model".

The four layers, scoped to what each can see:

| Layer | When | Scope | Owner |
|---|---|---|---|
| 1. image-time | `pack build` | the app image | this extension (`rdx_app`) + buildpack stack |
| 2. template-time | every `helm template` | rendered DSL | `rdx-opinion-bundle-example` library helpers |
| 3. promote-time | `rdx promote` | declared chart deps + rendered manifests | `rdx` CLI |
| 4. admission-time | cluster admission | live applies | cluster operator's Kubewarden policies |

Each layer catches what only it can see; later layers exist as
defense in depth, not as substitutes. A buildpack-time CVE check
cannot see `services[].type=mongodb`; a promote-time
`forbidden_charts` gate cannot see an unsigned base image.

### Planned Layer 1 gates

Scoped to the app image only:

- **CVE scan** — fail the build if grype/trivy finds critical or
  high CVEs in the app's deps or base layer. Severity threshold
  configurable via the corp overlay.
- **Image signature** — verify (or produce) a cosign signature
  against the corp KMS key. Pairs with Layer 3's `cosign_verify`
  for SUSE sub-chart images.
- **SBOM emission** — paketo already produces SBOMs; the extension
  surfaces them as a Tilt artefact and stages them for the
  promotion record.
- **License scan** — fail on copyleft licenses if the corp overlay
  forbids them.

### Status

Spec-only today: `rdx_app(...)` does not yet take gate-related
flags. Tracking issue: idefxH/tilt-extension-rdx#1 (to be
filed). The promote-time and template-time layers are already
shipping; this layer comes online when the RDX-SUSE buildpacks
do, since the corp-curated image gates need a corp-curated builder
to run inside.

## License

Apache-2.0
