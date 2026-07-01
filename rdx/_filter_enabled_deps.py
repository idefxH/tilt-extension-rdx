#!/usr/bin/env python3
"""
Strip the library Chart.yaml deps down to only the sub-charts the
project has enabled in its values. Cuts `helm dep update` work to
the minimum (one OCI pull per actually-used sub-chart) and dodges
SUSE rate-limits.

Usage: filter_enabled_deps.py <library-chart-dir> <values-file>...

Reads each values file, walks `rdx-library.<chart>.enabled` AND
top-level `<chart>.enabled` (rdx render's overlay shape), unions
the truthy chart names, then rewrites <library>/Chart.yaml keeping
only deps whose `name` matches.

Source-of-truth: <library>/Chart.yaml.full. Created on first run
from the live Chart.yaml. Filtering reads from .full every run so
re-enabling a previously-disabled chart still works.

Idempotent. Outputs one line per kept dep + the full count.
"""
import os
import re
import shutil
import sys


def _yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        # Fail loud, NOT silent. If PyYAML is missing the filter cannot
        # run, every helm dep update pulls every catalogued sub-chart,
        # and any HTTP-source repo (e.g. dex on charts.dexidp.io) that
        # isn't in `helm repo list` triggers a confusing `no cached
        # repository for helm-manager-<hash>` error several layers down.
        # Exit 2 so the caller's `|| { ... }` branch fires with a clear
        # remediation pointing the dev at python3 -m pip install pyyaml
        # in the python3 the host's Tilt actually invokes (which on
        # macOS may differ from the conda one in the dev's shell).
        sys.stderr.write(
            ("[filter_enabled_deps] PyYAML not installed in this python3 " +
             "({0}); install with: python3 -m pip install --break-system-packages pyyaml\n" +
             "(check `which python3` from the same env Tilt runs in — on macOS " +
             "the host's /usr/bin/python3 is often distinct from your shell's.)\n"
            ).format(sys.executable)
        )
        sys.exit(2)


def _is_deploy_provisioning(value):
    """True iff `provisioning` declares an in-cluster deploy.

    `local` (legacy) and `deploy` (current README) both deploy a
    workload (and therefore pull in operator sub-charts via
    chart_defaults). Absent → defaults to `deploy` (deploy-by-default
    matches CLI scaffolds). Everything else — `connect`, `shared`,
    `external`, future values — binds via the binding secret only and
    must NOT enable the sub-chart.
    """
    if value is None or value == "":
        return True
    return value in ("local", "deploy")


def _is_redacted(s):
    """True iff `s` is an rdx redacted-secret marker.

    Used as a fallback safety net. The ROOT cause of redacted markers
    leaking into `services[].type` is documented in `_is_overlay_file`
    below: rdx render projects services[] into the overlay with
    values_mapping applied, and any service whose values_mapping
    routes `type` through a binding-secret value gets the marker
    written back. This helper catches stragglers that slip past the
    overlay-file skip — e.g. a user values file that's been
    hand-edited from overlay output.
    """
    if not isinstance(s, str):
        return False
    return "[redacted " in s or "[REDACTED " in s


def _is_overlay_file(path):
    """True iff `path` is rdx render's generated values overlay.

    rdx render projects DSL `services[]` from the user's values.yaml
    into the overlay with `values_mapping` applied. For any service
    whose mapping routes a field through a binding-secret value, the
    overlay's projected field becomes a `[redacted secret <binding>-
    binding:<field>]` marker rather than the original DSL value (e.g.
    `type: '[redacted secret demo-dashboards-binding:type]'` instead
    of `type: grafana`).

    The overlay's clean role is to carry chart-level overrides
    (`<chart>.enabled: true`, `<chart>.image.tag: ...`) for the
    `helm()` step. Its `services[]` block is a derived projection —
    NOT the source of truth — so the filter must read services[] from
    the user's values.yaml ONLY. Otherwise the last-wins merge across
    the values stack lets the overlay's redacted entries clobber the
    clean ones from values.yaml, and every downstream consumer (the
    enabled-set, dep aliasing, branch-version patching, source
    rewriting, the k8s_resource registration loop) sees garbage.

    Path-shape: the overlay is conventionally at
    `<chart-dir>/.rdx/values.generated.yaml` (rdx-cli v0.1.38+) or
    `<chart-dir>/values.generated.yaml` (legacy, pre-v0.1.38). Both
    are recognised here.
    """
    if not path:
        return False
    p = path.replace("\\", "/")
    return (p.endswith("/.rdx/values.generated.yaml") or
            p.endswith("/values.generated.yaml") or
            p == ".rdx/values.generated.yaml" or
            p == "values.generated.yaml")


def _lib_key_for(library_dir):
    """Top-level values key the project nests its DSL under.

    `library_dir` is the vendored sub-chart dir (charts/<name>/); its
    basename is the dependency name, which is exactly the key Helm nests
    the sub-chart's values under. The library chart ships under different
    names across bundles, so the key is data-driven, NOT a constant —
    assuming a fixed name silently yields an empty enabled-set and the
    filter drops every dep (`kept 0/N`).
    """
    if library_dir:
        base = os.path.basename(os.path.normpath(library_dir))
        if base:
            return base
    return "rdx-library"


def _lib_block(data, lib_key):
    """The library sub-block from one values doc, tolerant of key drift.

    Prefers the detected `lib_key`; falls back to the conventional names
    in case a dep `alias` diverges from its vendored dir name.
    """
    for k in (lib_key, "rdx-library", "suse-library"):
        if k:
            blk = data.get(k)
            if isinstance(blk, dict):
                return blk
    return None


def enabled_charts(values_files, library_dir=None):
    """Union the chart names with .enabled=true across all values files.

    Three shapes supported:
      1. Static `<chart>.enabled: true` under `rdx-library` (legacy).
      2. rdx render overlay's `rdx-library.<chart>.enabled: true`.
      3. DSL `services[]` entries — `services[i].type` + `enabled: true`
         (the modern path, what `rdx service add` writes). Critical:
         this filter runs BEFORE `rdx render` regenerates the overlay,
         so we can't rely on shape #2 alone — services[] is the only
         source of truth at filter-time on a fresh project.

    Top-level `<chart>.enabled` is also checked as a fallback for the
    rare project that pre-dates the DSL.

    Operator-enabled charts: when `library_dir` is provided, also scan
    `dsl-mappings.yaml`'s `charts.<type>.versions[].chart_defaults` for
    any `<other-chart>.enabled: true` keys, and union those chart names
    too. Required because DSL type names (e.g. `cnpg`) can differ from
    the operator sub-chart they pull in (e.g. `cloudnative-pg`). On a
    cold start (CI), values.generated.yaml is empty so the chart_defaults
    haven't been materialized into the overlay yet — without this lookup
    the operator dep gets filtered out and `k8s_resource` references to
    it fail at runtime with `unknown resource`.
    """
    yaml = _yaml()
    enabled = set()
    lib_key = _lib_key_for(library_dir)
    SKIP_TOP = {lib_key, "rdx-library", "suse-library", "global", "image",
                "ingress", "imagePullSecrets", "resources", "probes",
                "metrics", "podAnnotations", "service", "name", "replicas",
                "port", "apiVersion", "kind"}
    for path in values_files:
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        is_overlay = _is_overlay_file(path)
        # Shape 1+2: under <lib-key>.<chart>.enabled — read from ALL
        # files, INCLUDING the overlay. The overlay's whole point is to
        # carry chart-level `<chart>.enabled` overrides written by
        # `rdx render` for each service[] entry it processed.
        sl = _lib_block(data, lib_key)
        if isinstance(sl, dict):
            for name, sub in sl.items():
                if isinstance(sub, dict) and sub.get("enabled") is True:
                    enabled.add(name)
            # Shape 3: services[] DSL — services[i].type + enabled. The
            # CLI defaults services[].enabled to false in the scaffold,
            # so we only count entries the dev has explicitly flipped to
            # true. Same iteration the library helpers and `rdx render`
            # walk; staying consistent here matters.
            #
            # OVERLAY SKIP (root-cause fix for the redacted-type bug):
            # the overlay is a PROJECTION of services[] with
            # values_mapping applied. When a mapping routes `type`
            # through a binding-secret value, the overlay writes
            # `type: '[redacted secret demo-<binding>-binding:type]'`
            # instead of the plain DSL type. Reading services[] from
            # the overlay then lets the marker into the enabled set,
            # which propagates into Chart.yaml dep names and downstream
            # k8s_resource() lookups. The user's values.yaml is the
            # source of truth for services[]; the overlay is a
            # downstream artefact and must NOT be re-read here.
            #
            # Provisioning gate: only `local`/`deploy` modes deploy an
            # in-cluster workload (and pull in operator sub-charts via
            # chart_defaults below). `connect`/`shared`/`external` modes
            # bind to a pre-existing instance via the binding secret only
            # — they MUST NOT add the chart to the enabled set, otherwise
            # `helm dep update` pulls the operator chart and `helm install`
            # deploys it (e.g. cloudnative-pg operator for a postgresql
            # service that's actually pointing at an external DB).
            services = [] if is_overlay else (sl.get("services") or [])
            if isinstance(services, list):
                # Count types for multi-instance alias computation (#24).
                # Only count deploy-mode entries — connect-mode siblings
                # don't compete for an alias slot. Redacted-type entries
                # are also skipped so the redacted marker never reaches
                # the enabled set (which would propagate into Chart.yaml
                # dep names and `helm dep update` failures).
                type_counts = {}
                for entry in services:
                    if not isinstance(entry, dict):
                        continue
                    if not _is_deploy_provisioning(entry.get("provisioning")):
                        continue
                    t = entry.get("type")
                    if not t or _is_redacted(t):
                        continue
                    type_counts[t] = type_counts.get(t, 0) + 1
                for entry in services:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("enabled", True) is False:
                        continue
                    if not _is_deploy_provisioning(entry.get("provisioning")):
                        continue
                    chart_type = entry.get("type")
                    binding = entry.get("binding", "")
                    if not chart_type:
                        continue
                    if _is_redacted(chart_type):
                        sys.stderr.write(
                            ("[filter_enabled_deps] skipping services[binding={0!r}]: " +
                             "type={1!r} is a redacted secret marker. The DSL type " +
                             "must be a plain string (e.g. 'grafana'); a redacted " +
                             "value here usually means dsl-mappings.yaml chained " +
                             "`services[].type` through a binding-secret value_mapping. " +
                             "Sub-chart is not added to the enabled set; fix the " +
                             "mapping in the bundle to route `type` from project " +
                             "values, not from a secret.\n").format(binding, chart_type)
                        )
                        continue
                    enabled.add(chart_type)
                    # Multi-instance: also add the aliased name.
                    if type_counts.get(chart_type, 0) > 1 and binding:
                        enabled.add(chart_type + "-" + binding)
        # Top-level fallback (legacy / non-DSL projects).
        for name, sub in data.items():
            if name in SKIP_TOP:
                continue
            if isinstance(sub, dict) and sub.get("enabled") is True:
                enabled.add(name)

    # Operator chart_defaults expansion: for each enabled DSL type, pull
    # any `<chart>.enabled: true` keys out of its dsl-mappings entry's
    # chart_defaults and union those chart names too. Snapshot the set
    # first so we only expand from user-declared types, not transitively.
    if library_dir:
        dsl_path = os.path.join(library_dir, "dsl-mappings.yaml")
        if os.path.isfile(dsl_path):
            try:
                with open(dsl_path) as f:
                    dsl = yaml.safe_load(f) or {}
            except Exception:
                dsl = {}
            charts_dsl = dsl.get("charts") or {}
            for t in list(enabled):
                entry = charts_dsl.get(t) or {}
                for ver in (entry.get("versions") or []):
                    if not isinstance(ver, dict):
                        continue
                    for k, v in (ver.get("chart_defaults") or {}).items():
                        if k.endswith(".enabled") and v is True:
                            enabled.add(k[:-len(".enabled")])
    return enabled


def main(library_dir, *values_files):
    chart_yaml = os.path.join(library_dir, "Chart.yaml")
    full_backup = chart_yaml + ".full"
    lib_key = _lib_key_for(library_dir)
    if not os.path.isfile(chart_yaml):
        return

    # First run: snapshot the live Chart.yaml as the source-of-truth.
    if not os.path.isfile(full_backup):
        shutil.copy2(chart_yaml, full_backup)

    yaml = _yaml()
    with open(full_backup) as f:
        full = yaml.safe_load(f) or {}

    full_deps = full.get("dependencies") or []
    if not full_deps:
        return  # nothing to filter

    enabled = enabled_charts(values_files, library_dir=library_dir)

    # Multi-instance aliasing (#24): expand deps for types with
    # multiple bindings. Read services[] to find multi-instance types,
    # then duplicate the original dep entry with per-binding aliases.
    # SYNC: alias formula is <type>-<binding>. Must match
    # rdx-cli/internal/render/alias.go:ComputeAliases.
    expanded_deps = list(full_deps)
    multi_aliases = {}  # type → [alias1, alias2, ...]
    for path in values_files:
        if not os.path.isfile(path):
            continue
        # See _is_overlay_file: services[] in the overlay is a projection
        # with values_mapping applied (potentially with redacted types) —
        # always read services[] from the user's values.yaml only.
        if _is_overlay_file(path):
            continue
        try:
            with open(path) as f:
                data = _yaml().safe_load(f) or {}
        except Exception:
            continue
        sl = _lib_block(data, lib_key)
        if not isinstance(sl, dict):
            continue
        services = sl.get("services") or []
        if not isinstance(services, list):
            continue
        type_bindings = {}
        for entry in services:
            if not isinstance(entry, dict):
                continue
            # Connect/shared/external entries don't add a sub-chart, so
            # they MUST NOT count toward multi-instance alias splitting.
            # Otherwise a (deploy, connect) sibling pair would aliase the
            # one deploy dep to <type>-<binding1>, drop it from `enabled`
            # (which still has the bare type), and filter it out — the
            # sub-chart silently disappears.
            if not _is_deploy_provisioning(entry.get("provisioning")):
                continue
            if entry.get("enabled", True) is False:
                continue
            t = entry.get("type", "")
            b = entry.get("binding", "")
            # Redacted types can't be aliased — the resulting key would
            # propagate the marker into `multi_aliases` and ultimately
            # into Chart.yaml dep names. Skip; enabled_charts already
            # warned.
            if _is_redacted(t) or _is_redacted(b):
                continue
            if t and b:
                type_bindings.setdefault(t, []).append(b)
        for t, bindings in type_bindings.items():
            if len(bindings) > 1:
                multi_aliases[t] = [t + "-" + b for b in bindings]

    if multi_aliases:
        new_deps = []
        for d in expanded_deps:
            ename = d.get("alias", d.get("name"))
            if ename in multi_aliases:
                # Replace single dep with N aliased deps.
                for alias in multi_aliases[ename]:
                    aliased = dict(d)
                    aliased["alias"] = alias
                    aliased["condition"] = alias + ".enabled"
                    new_deps.append(aliased)
            else:
                new_deps.append(d)
        expanded_deps = new_deps

    # Branch version patching (DO-0005 Phase 2): when a service declares
    # branch: "16" and dsl-mappings has branches."16".chart_version, patch
    # the dep's version: field so helm dep update pulls the correct chart.
    dsl_path = os.path.join(library_dir, "dsl-mappings.yaml")
    if os.path.isfile(dsl_path):
        try:
            with open(dsl_path) as f:
                dsl = _yaml().safe_load(f) or {}
        except Exception:
            dsl = {}
        charts_dsl = dsl.get("charts") or {}
        branch_versions = {}  # chart_type -> chart_version from branch
        for path in values_files:
            if not os.path.isfile(path):
                continue
            # Skip the overlay — services[] is a derived projection there.
            if _is_overlay_file(path):
                continue
            try:
                with open(path) as f:
                    data = _yaml().safe_load(f) or {}
            except Exception:
                continue
            sl = _lib_block(data, lib_key)
            if not isinstance(sl, dict):
                continue
            for entry in (sl.get("services") or []):
                if not isinstance(entry, dict):
                    continue
                t = entry.get("type", "")
                if _is_redacted(t):
                    continue  # fallback guard if overlay-skip missed
                branch = str(entry.get("branch", ""))
                if t and branch and t in charts_dsl:
                    branches = charts_dsl[t].get("branches") or {}
                    if branch in branches:
                        cv = branches[branch].get("chart_version", "")
                        if cv:
                            branch_versions[t] = cv
        for d in expanded_deps:
            dep_name = d.get("alias", d.get("name", ""))
            base_type = dep_name.split("-")[0] if "-" in dep_name else dep_name
            if base_type in branch_versions:
                d["version"] = branch_versions[base_type]

    # Per-service source rewriting (DO-0012): services[] can override
    # the project source with a per-service `source:` field. Read each
    # service's source, look up the target source in dsl-mappings, and
    # rewrite the dep's repo/name/version accordingly.
    if charts_dsl:
        service_sources = {}  # chart_type -> source
        project_source = ""
        project_dir = os.path.dirname(library_dir)
        project_root = os.path.dirname(project_dir)
        project_yaml = os.path.join(project_root, ".rdx", "project.yaml")
        if os.path.isfile(project_yaml):
            try:
                with open(project_yaml) as f:
                    proj_meta = _yaml().safe_load(f) or {}
                project_source = proj_meta.get("chart_source", "")
            except Exception:
                pass
        for path in values_files:
            if not os.path.isfile(path):
                continue
            # Skip overlay; services[].source lives in values.yaml.
            if _is_overlay_file(path):
                continue
            try:
                with open(path) as f:
                    data = _yaml().safe_load(f) or {}
            except Exception:
                continue
            sl = _lib_block(data, lib_key)
            if not isinstance(sl, dict):
                continue
            for entry in (sl.get("services") or []):
                if not isinstance(entry, dict):
                    continue
                t = entry.get("type", "")
                if _is_redacted(t):
                    continue  # fallback guard if overlay-skip missed
                svc_source = entry.get("source", "")
                if t and svc_source:
                    service_sources[t] = svc_source

        for d in expanded_deps:
            dep_alias = d.get("alias", "")
            dep_name = d.get("name", "")
            chart_type = dep_alias or dep_name
            # infra deps (e.g. cloudnative-pg operator) follow the source
            # of their parent DSL type (cnpg). Check infra_only flag.
            chart_entry_meta = charts_dsl.get(chart_type, {})
            parent_type = chart_type
            if chart_entry_meta.get("infra_only"):
                # Find DSL type that enables this infra dep via chart_defaults
                for svc_t, svc_src in service_sources.items():
                    svc_entry = charts_dsl.get(svc_t, {})
                    for ver in (svc_entry.get("versions") or []):
                        defaults = ver.get("chart_defaults") or {}
                        if any(chart_type in str(k) for k in defaults):
                            parent_type = svc_t
                            break
            svc_source = service_sources.get(parent_type, project_source)
            if not svc_source:
                continue
            chart_entry = charts_dsl.get(chart_type, {})
            sources = chart_entry.get("sources", {})
            target_src = sources.get(svc_source, {})
            if not target_src:
                continue
            target_repo = target_src.get("chart_ref", "")
            target_name = target_src.get("chart_name", "")
            if not target_repo:
                continue
            if d.get("repository", "") != target_repo:
                d["repository"] = target_repo
            if target_name and dep_name != target_name:
                d["name"] = target_name
                if not dep_alias:
                    d["alias"] = chart_type
            elif not target_name and dep_alias and dep_name != chart_type:
                d["name"] = chart_type
                d.pop("alias", None)
            if svc_source == "community":
                ver = d.get("version", "")
                base = re.sub(r'-\d+\.\d+$', '', ver)
                if base != ver:
                    d["version"] = base

    if not enabled:
        kept = []
    else:
        kept = [d for d in expanded_deps if d.get("alias", d.get("name")) in enabled]

    # Final defense: even if a redacted marker slipped past every upstream
    # filter (corrupted Chart.yaml.full, third-party rewrite, future code
    # path), drop any dep whose name or alias contains it. `helm dep update`
    # would fail with a confusing message on such an entry, and we'd rather
    # the user see the warning here than try to debug a chart-resolution
    # error three layers down.
    safe_kept = []
    for d in kept:
        n = d.get("name", "")
        a = d.get("alias", "")
        if _is_redacted(n) or _is_redacted(a):
            sys.stderr.write(
                ("[filter_enabled_deps] dropping dep with redacted-secret " +
                 "marker in name/alias: name={0!r}, alias={1!r}. This usually " +
                 "means a per-service source-rewrite or Chart.yaml.full backup " +
                 "captured a redacted value; delete <library>/Chart.yaml.full " +
                 "and re-run to recover from a clean snapshot.\n").format(n, a)
            )
            continue
        safe_kept.append(d)
    kept = safe_kept

    out = dict(full)  # shallow copy
    out["dependencies"] = kept

    new_content = yaml.safe_dump(out, default_flow_style=False, sort_keys=False)
    # IDEMPOTENT WRITE: only touch Chart.yaml if the content actually
    # differs. Without this guard, Tilt sees Chart.yaml change every
    # reload (because we always rewrite), reloads the Tiltfile (the chart
    # tree is in its watch set), which re-runs this script, which writes
    # again — infinite loop. Tilt's fast-path
    # `Chart.lock newer than Chart.yaml` also never triggers because
    # we keep bumping Chart.yaml's mtime. See tilt-extension #URGENT.
    try:
        with open(chart_yaml) as f:
            cur_content = f.read()
    except OSError:
        cur_content = None
    wrote = (cur_content != new_content)
    if wrote:
        with open(chart_yaml, "w") as f:
            f.write(new_content)
        # When repos changed (source switch), delete stale .tgz files
        # and Chart.lock to force helm dep update to re-pull.
        charts_subdir = os.path.join(library_dir, "charts")
        if os.path.isdir(charts_subdir):
            for tgz in os.listdir(charts_subdir):
                if tgz.endswith(".tgz"):
                    os.remove(os.path.join(charts_subdir, tgz))
        lock = os.path.join(library_dir, "Chart.lock")
        if os.path.isfile(lock):
            os.remove(lock)

    skipped = len(full_deps) - len(kept)
    suffix = "" if wrote else " (unchanged on disk)"
    print(
        ("[rdx] filter_enabled_deps: kept {0}/{1} sub-chart(s) " +
         "[{2}]; skipped {3} disabled{4}").format(
            len(kept), len(full_deps),
            ", ".join(sorted([d.get("name", "?") for d in kept])) or "none",
            skipped, suffix,
        ),
        flush=True,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: filter_enabled_deps.py <library-chart-dir> <values-file>...",
              file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
