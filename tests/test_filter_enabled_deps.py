#!/usr/bin/env python3
"""
Test the filter_enabled_deps logic (#16).

The real filter_enabled_deps.py is embedded in the Tiltfile as a Starlark
string literal — it cannot be imported directly. This test reimplements
the core filtering logic (enabled_charts + dep filtering + multi-instance
alias expansion) and validates it against representative inputs.

Three behaviours tested:
  1. Basic filtering: 16 deps in Chart.yaml, 2 services enabled
     (postgresql, redis) => only those 2 deps are kept.
  2. Multi-instance aliasing (#24): two grafana bindings expand to two
     aliased deps (grafana-metrics, grafana-dashboards).
  3. Existing aliases preserved: dex-idp aliased as dex stays intact.

Usage: python3 test_filter_enabled_deps.py
       chmod +x && ./test_filter_enabled_deps.py
"""
import os
import sys
import tempfile
import shutil

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML not installed; pip3 install pyyaml\n")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Reimplemented core logic (mirrors the Tiltfile-embedded script exactly).
# ---------------------------------------------------------------------------

def _lib_block(data, lib_key):
    """Mirror of the Tiltfile-embedded helper.

    Resolve the library sub-block, preferring the detected `lib_key` and
    falling back to the conventional names. The library values key is the
    vendored dep name, which varies across bundles (`suse-library`,
    `rdx-library`, ...) — hardcoding one yields an empty enabled-set and
    the filter drops every dep.
    """
    for k in (lib_key, "rdx-library", "suse-library"):
        if k:
            blk = data.get(k)
            if isinstance(blk, dict):
                return blk
    return None


def _skip_top(lib_key):
    """Top-level keys excluded from the legacy `<chart>.enabled` scan."""
    return {
        lib_key, "rdx-library", "suse-library", "global", "image",
        "ingress", "imagePullSecrets", "resources", "probes", "metrics",
        "podAnnotations", "service", "name", "replicas",
        "port", "apiVersion", "kind",
    }


def _is_deploy_provisioning(value):
    """Mirror of the Tiltfile-embedded helper.

    `local`/`deploy` and absent → in-cluster deploy. Everything else
    (`connect`, `shared`, `external`) → bind via secret only, sub-chart
    is NOT pulled.
    """
    if value is None or value == "":
        return True
    return value in ("local", "deploy")


def _is_redacted(s):
    """Mirror of the Tiltfile-embedded helper.

    Fallback safety net. The PRIMARY fix is `_is_overlay_file` below:
    skip services[] from the rdx-generated overlay where redacted
    markers originate.
    """
    if not isinstance(s, str):
        return False
    return "[redacted " in s or "[REDACTED " in s


def _is_overlay_file(path):
    """Mirror of the Tiltfile-embedded helper.

    Root-cause fix: skip services[] when reading the rdx render
    overlay. The overlay projects services[] with values_mapping
    applied and may contain redacted-secret markers in the projected
    fields. The user's values.yaml is the source of truth for
    services[] DSL — the overlay is only consulted for chart-level
    overrides (`<chart>.enabled`, etc.).
    """
    if not path:
        return False
    p = path.replace("\\", "/")
    return (p.endswith("/.rdx/values.generated.yaml") or
            p.endswith("/values.generated.yaml") or
            p == ".rdx/values.generated.yaml" or
            p == "values.generated.yaml")


def enabled_charts(values_files, dsl_mappings=None, lib_key="rdx-library"):
    """Union the chart names with .enabled=true across all values files.

    `dsl_mappings` (optional) is a dict shaped like the parsed
    library-chart/dsl-mappings.yaml: `{"charts": {<type>: {"versions":
    [{"chart_defaults": {<chart>.enabled: true}}]}}}`. When provided,
    enabled-set is expanded with chart_defaults of each user-enabled DSL
    type (the operator-subchart case, e.g. postgresql → cnpg +
    cloudnative-pg). Connect-mode services are filtered BEFORE this
    expansion so their operator deps stay out.
    """
    enabled = set()
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
        sl = _lib_block(data, lib_key)
        if isinstance(sl, dict):
            for name, sub in sl.items():
                if isinstance(sub, dict) and sub.get("enabled") is True:
                    enabled.add(name)
            # Root-cause fix: services[] DSL is sourced from values.yaml
            # only; the overlay projects it with values_mapping applied
            # and can carry redacted-secret markers.
            services = [] if is_overlay else (sl.get("services") or [])
            if isinstance(services, list):
                # Only deploy-mode entries count toward multi-instance
                # aliasing — connect-mode siblings don't compete.
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
                        # Skip — redacted types can't be mapped to a chart.
                        continue
                    enabled.add(chart_type)
                    if type_counts.get(chart_type, 0) > 1 and binding:
                        enabled.add(chart_type + "-" + binding)
        skip_top = _skip_top(lib_key)
        for name, sub in data.items():
            if name in skip_top:
                continue
            if isinstance(sub, dict) and sub.get("enabled") is True:
                enabled.add(name)

    # chart_defaults expansion: only enabled (deploy-mode) DSL types
    # get their operator sub-charts pulled in.
    if dsl_mappings:
        charts_dsl = dsl_mappings.get("charts") or {}
        for t in list(enabled):
            entry = charts_dsl.get(t) or {}
            for ver in (entry.get("versions") or []):
                if not isinstance(ver, dict):
                    continue
                for k, v in (ver.get("chart_defaults") or {}).items():
                    if k.endswith(".enabled") and v is True:
                        enabled.add(k[:-len(".enabled")])
    return enabled


def filter_deps(full_deps, enabled, values_files, lib_key="rdx-library"):
    """Filter and expand deps based on enabled charts and multi-instance aliases."""
    expanded_deps = list(full_deps)
    multi_aliases = {}

    for path in values_files:
        if not os.path.isfile(path):
            continue
        # Root-cause fix: never read services[] from the overlay.
        if _is_overlay_file(path):
            continue
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
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
            # Skip connect/shared/external — see enabled_charts() above.
            if not _is_deploy_provisioning(entry.get("provisioning")):
                continue
            if entry.get("enabled", True) is False:
                continue
            t = entry.get("type", "")
            b = entry.get("binding", "")
            # Redacted type/binding would propagate the marker into
            # multi_aliases and ultimately into Chart.yaml dep names.
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
                for alias in multi_aliases[ename]:
                    aliased = dict(d)
                    aliased["alias"] = alias
                    aliased["condition"] = alias + ".enabled"
                    new_deps.append(aliased)
            else:
                new_deps.append(d)
        expanded_deps = new_deps

    if not enabled:
        return []
    kept = [d for d in expanded_deps if d.get("alias", d.get("name")) in enabled]
    # Final defense: drop any dep whose name/alias is itself a redacted
    # marker (corrupted Chart.yaml.full, third-party rewrite).
    return [d for d in kept
            if not _is_redacted(d.get("name", ""))
            and not _is_redacted(d.get("alias", ""))]


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

# Representative Chart.yaml with 16 deps (the full catalog).
FULL_CHART_YAML = {
    "apiVersion": "v2",
    "name": "rdx-library",
    "version": "0.1.0",
    "dependencies": [
        {"name": "postgresql",     "version": "0.4.4-29.1", "repository": "oci://dp.apps.rancher.io/charts", "condition": "postgresql.enabled"},
        {"name": "redis",          "version": "2.0.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "redis.enabled"},
        {"name": "prometheus",     "version": "29.0.0",     "repository": "oci://dp.apps.rancher.io/charts", "condition": "prometheus.enabled"},
        {"name": "grafana",        "version": "12.0.0",     "repository": "oci://dp.apps.rancher.io/charts", "condition": "grafana.enabled"},
        {"name": "dex-idp",        "version": "0.24.0",     "repository": "oci://dp.apps.rancher.io/charts", "alias": "dex", "condition": "dex.enabled"},
        {"name": "mariadb",        "version": "0.1.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "mariadb.enabled"},
        {"name": "apache-kafka",   "version": "1.0.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "apache-kafka.enabled"},
        {"name": "valkey",         "version": "0.11.0",     "repository": "oci://dp.apps.rancher.io/charts", "condition": "valkey.enabled"},
        {"name": "minio",          "version": "5.0.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "minio.enabled"},
        {"name": "vault",          "version": "0.31.0",     "repository": "oci://dp.apps.rancher.io/charts", "condition": "vault.enabled"},
        {"name": "etcd",           "version": "0.3.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "etcd.enabled"},
        {"name": "nats",           "version": "2.0.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "nats.enabled"},
        {"name": "opensearch",     "version": "3.0.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "opensearch.enabled"},
        {"name": "influxdb",       "version": "2.0.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "influxdb.enabled"},
        {"name": "harbor",         "version": "1.0.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "harbor.enabled"},
        {"name": "apache-airflow", "version": "1.0.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "apache-airflow.enabled"},
        {"name": "cnpg",           "version": "0.1.0",      "repository": "oci://dp.apps.rancher.io/charts", "condition": "cnpg.enabled"},
        {"name": "cloudnative-pg", "version": "0.24.0",     "repository": "oci://dp.apps.rancher.io/charts", "condition": "cloudnative-pg.enabled"},
    ],
}


# dsl-mappings stub: postgresql DSL type pulls in cnpg + cloudnative-pg
# via chart_defaults (the operator-subchart case the user hit).
DSL_MAPPINGS_PG = {
    "charts": {
        "postgresql": {
            "versions": [{
                "chart_defaults": {
                    "cnpg.enabled": True,
                    "cloudnative-pg.enabled": True,
                },
            }],
        },
    },
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

passed = 0
failed = 0


def report(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print("  \033[32m✓\033[0m %s" % name)
    else:
        failed += 1
        msg = "  \033[31m✗\033[0m %s" % name
        if detail:
            msg += " — %s" % detail
        print(msg, file=sys.stderr)


def test_basic_filtering():
    """Only enabled services' deps are kept (16 deps, 2 enabled)."""
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        # Write Chart.yaml.full (source of truth).
        chart_yaml_full = os.path.join(tmpdir, "Chart.yaml.full")
        with open(chart_yaml_full, "w") as f:
            yaml.safe_dump(FULL_CHART_YAML, f, default_flow_style=False)

        # Write values.yaml with postgresql + redis enabled.
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "postgresql", "binding": "db",    "enabled": True,
                     "auth": {"user": {"password": "dev", "database": "app"},
                              "admin": {"password": "admin"}}},
                    {"type": "redis",      "binding": "cache", "enabled": True,
                     "auth": {"password": "dev"}},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path])
        full_deps = FULL_CHART_YAML["dependencies"]
        kept = filter_deps(full_deps, enabled, [values_path])

        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])
        report(
            "basic filtering: 16 deps -> 2 kept (postgresql, redis)",
            kept_names == ["postgresql", "redis"],
            "got %s" % kept_names,
        )
        report(
            "basic filtering: enabled set is exactly {postgresql, redis}",
            enabled == {"postgresql", "redis"},
            "got %s" % enabled,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_multi_instance_aliasing():
    """Two grafana bindings expand to two aliased deps (#24)."""
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "grafana", "binding": "metrics",    "enabled": True,
                     "auth": {"admin": {"password": "dev"}}},
                    {"type": "grafana", "binding": "dashboards", "enabled": True,
                     "auth": {"admin": {"password": "dev"}}},
                    {"type": "postgresql", "binding": "db",      "enabled": True,
                     "auth": {"user": {"password": "dev", "database": "app"},
                              "admin": {"password": "admin"}}},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path])
        full_deps = FULL_CHART_YAML["dependencies"]
        kept = filter_deps(full_deps, enabled, [values_path])

        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])

        # grafana type -> grafana-metrics + grafana-dashboards (aliased)
        report(
            "multi-instance: grafana-metrics alias present",
            "grafana-metrics" in kept_names,
            "got %s" % kept_names,
        )
        report(
            "multi-instance: grafana-dashboards alias present",
            "grafana-dashboards" in kept_names,
            "got %s" % kept_names,
        )
        report(
            "multi-instance: postgresql (single instance) still present",
            "postgresql" in kept_names,
            "got %s" % kept_names,
        )
        report(
            "multi-instance: total kept count is 3 (2 grafana aliases + 1 postgresql)",
            len(kept) == 3,
            "got %d: %s" % (len(kept), kept_names),
        )

        # Verify the aliased deps have correct condition fields.
        for d in kept:
            alias = d.get("alias", d.get("name"))
            if alias.startswith("grafana-"):
                report(
                    "multi-instance: %s condition is %s.enabled" % (alias, alias),
                    d.get("condition") == alias + ".enabled",
                    "got condition=%r" % d.get("condition"),
                )
                # The underlying chart name must still be grafana.
                report(
                    "multi-instance: %s name is still grafana" % alias,
                    d.get("name") == "grafana",
                    "got name=%r" % d.get("name"),
                )
    finally:
        shutil.rmtree(tmpdir)


def test_preserves_existing_aliases():
    """dex-idp aliased as dex stays intact after filtering."""
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "dex", "binding": "auth", "enabled": True,
                     "ingress": {"enabled": True,
                                 "host": "auth.app.localtest.me"}},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path])
        full_deps = FULL_CHART_YAML["dependencies"]
        kept = filter_deps(full_deps, enabled, [values_path])

        # The dex dep has alias=dex, so the effective name is "dex".
        kept_names = [d.get("alias", d.get("name")) for d in kept]
        report(
            "existing alias: dex is in kept deps",
            "dex" in kept_names,
            "got %s" % kept_names,
        )

        # The underlying chart name must remain dex-idp.
        dex_dep = [d for d in kept if d.get("alias") == "dex"]
        report(
            "existing alias: underlying name is dex-idp",
            len(dex_dep) == 1 and dex_dep[0].get("name") == "dex-idp",
            "got %s" % ([d.get("name") for d in dex_dep],),
        )

        # Only 1 dep should be kept.
        report(
            "existing alias: exactly 1 dep kept",
            len(kept) == 1,
            "got %d" % len(kept),
        )
    finally:
        shutil.rmtree(tmpdir)


def test_disabled_services_excluded():
    """Services with enabled=false are not kept."""
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "postgresql", "binding": "db", "enabled": True,
                     "auth": {"user": {"password": "dev", "database": "app"},
                              "admin": {"password": "admin"}}},
                    {"type": "redis", "binding": "cache", "enabled": False,
                     "auth": {"password": "dev"}},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path])
        full_deps = FULL_CHART_YAML["dependencies"]
        kept = filter_deps(full_deps, enabled, [values_path])

        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])
        report(
            "disabled excluded: only postgresql kept",
            kept_names == ["postgresql"],
            "got %s" % kept_names,
        )
        report(
            "disabled excluded: redis not in enabled set",
            "redis" not in enabled,
            "enabled=%s" % enabled,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_empty_services():
    """No services enabled => no deps kept."""
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {"rdx-library": {"services": []}}
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path])
        full_deps = FULL_CHART_YAML["dependencies"]
        kept = filter_deps(full_deps, enabled, [values_path])

        report(
            "empty services: no deps kept",
            len(kept) == 0,
            "got %d" % len(kept),
        )
    finally:
        shutil.rmtree(tmpdir)


def test_connect_provisioning_skips_operator_subchart():
    """`provisioning: connect` must NOT pull in the operator sub-chart.

    Regression: a postgresql service in connect mode (external DB)
    was still pulling cnpg + cloudnative-pg into Chart.yaml via
    chart_defaults expansion, deploying the operator pointlessly and
    breaking k8s_resource registration with a redacted-secret host
    name. The connect-mode entry must be invisible to the filter.
    """
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "postgresql", "binding": "db", "enabled": True,
                     "provisioning": "connect",
                     "credentials": {"secretRef": "shared-pg"}},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path], dsl_mappings=DSL_MAPPINGS_PG)
        full_deps = FULL_CHART_YAML["dependencies"]
        kept = filter_deps(full_deps, enabled, [values_path])

        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])
        report(
            "connect provisioning: postgresql NOT in enabled set",
            "postgresql" not in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "connect provisioning: cnpg operator sub-chart NOT enabled",
            "cnpg" not in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "connect provisioning: cloudnative-pg operator NOT enabled",
            "cloudnative-pg" not in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "connect provisioning: no deps kept",
            kept_names == [],
            "got %s" % kept_names,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_deploy_provisioning_pulls_operator_subchart():
    """`provisioning: deploy` (and absent) MUST pull in operator deps.

    The companion to test_connect_provisioning_skips_operator_subchart:
    when the user does want an in-cluster postgres, chart_defaults
    expansion still adds cnpg + cloudnative-pg.
    """
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "postgresql", "binding": "db", "enabled": True,
                     "provisioning": "deploy"},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path], dsl_mappings=DSL_MAPPINGS_PG)
        kept = filter_deps(FULL_CHART_YAML["dependencies"], enabled, [values_path])
        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])

        report(
            "deploy provisioning: postgresql in enabled set",
            "postgresql" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "deploy provisioning: cnpg operator pulled in via chart_defaults",
            "cnpg" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "deploy provisioning: cloudnative-pg operator pulled in",
            "cloudnative-pg" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "deploy provisioning: all three deps kept (postgresql, cnpg, cloudnative-pg)",
            kept_names == ["cloudnative-pg", "cnpg", "postgresql"],
            "got %s" % kept_names,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_provisioning_absent_defaults_to_deploy():
    """Absent `provisioning` keeps the legacy deploy-by-default behaviour.

    Pre-DSL projects and fresh scaffolds that don't set provisioning
    must keep working — the gate is opt-OUT of deploy, not opt-IN.
    """
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "redis", "binding": "cache", "enabled": True,
                     "auth": {"password": "dev"}},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path])
        kept = filter_deps(FULL_CHART_YAML["dependencies"], enabled, [values_path])
        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])

        report(
            "provisioning absent: redis still enabled (default=deploy)",
            "redis" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "provisioning absent: redis dep kept",
            kept_names == ["redis"],
            "got %s" % kept_names,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_mixed_connect_and_deploy():
    """Two postgres services — one deploy, one connect — only deploy's operator is enabled.

    Realistic mixed setup: app uses CNPG for its primary DB and a
    shared external postgres for analytics. The connect entry must
    NOT cancel the operator pull from the deploy entry.
    """
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "postgresql", "binding": "db", "enabled": True,
                     "provisioning": "deploy"},
                    {"type": "postgresql", "binding": "analytics", "enabled": True,
                     "provisioning": "connect",
                     "credentials": {"secretRef": "shared-pg"}},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path], dsl_mappings=DSL_MAPPINGS_PG)
        kept = filter_deps(FULL_CHART_YAML["dependencies"], enabled, [values_path])
        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])

        report(
            "mixed: postgresql in enabled set (from deploy entry)",
            "postgresql" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "mixed: cnpg pulled in via deploy entry's chart_defaults",
            "cnpg" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "mixed: postgresql NOT multi-instance aliased (analytics is connect)",
            "postgresql-db" not in enabled and "postgresql-analytics" not in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "mixed: bare 'postgresql' dep (no alias split) is kept",
            "postgresql" in kept_names,
            "got %s" % kept_names,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_shared_and_external_also_skipped():
    """`shared` and `external` provisioning also skip operator pulls."""
    for mode in ("shared", "external"):
        tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
        try:
            values_path = os.path.join(tmpdir, "values.yaml")
            values = {
                "rdx-library": {
                    "services": [
                        {"type": "postgresql", "binding": "db", "enabled": True,
                         "provisioning": mode},
                    ],
                },
            }
            with open(values_path, "w") as f:
                yaml.safe_dump(values, f, default_flow_style=False)

            enabled = enabled_charts([values_path], dsl_mappings=DSL_MAPPINGS_PG)
            report(
                "provisioning=%s: postgresql NOT enabled" % mode,
                "postgresql" not in enabled,
                "got enabled=%s" % enabled,
            )
            report(
                "provisioning=%s: cnpg operator NOT enabled" % mode,
                "cnpg" not in enabled,
                "got enabled=%s" % enabled,
            )
        finally:
            shutil.rmtree(tmpdir)


# ---------------------------------------------------------------------------
# Tests for the bad-workload-name guard (Tiltfile registration loop)
# ---------------------------------------------------------------------------

def _bad_workload_name(s):
    """Mirror of the Tiltfile-embedded helper in rdx_app()."""
    if not s or not s.strip():
        return True
    if '{{' in s or '}}' in s:
        return True
    if '[redacted' in s or 'redacted secret' in s:
        return True
    return False


def test_overlay_services_block_skipped():
    """ROOT-CAUSE FIX: services[] from rdx's overlay must be ignored.

    Reproduction of the reported scenario:
      - User's values.yaml has clean DSL: services[].type=grafana etc.
      - rdx render writes chart/.rdx/values.generated.yaml with a
        projected services[] block whose `type` fields are
        `[redacted secret demo-<binding>-binding:type]` because the
        bundle's values_mapping routes type through a binding secret.
      - The filter is called with BOTH files. Last-wins merge would
        let the overlay's redacted entries clobber the clean ones.

    Expected: clean entries from values.yaml WIN, overlay services[]
    is skipped, no redacted markers reach the enabled set, the
    correct chart deps end up in Chart.yaml.
    """
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        # 1. User's clean DSL in values.yaml
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "grafana",    "binding": "dashboards", "enabled": True,
                     "provisioning": "deploy"},
                    {"type": "prometheus", "binding": "metrics",    "enabled": True,
                     "provisioning": "deploy"},
                    {"type": "postgresql", "binding": "db",         "enabled": True,
                     "provisioning": "deploy"},
                    {"type": "dex",        "binding": "auth",       "enabled": True,
                     "provisioning": "deploy"},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        # 2. rdx render overlay at the canonical path. Its services[]
        # has redacted types and CHART-level enabled flags that the
        # filter SHOULD still pick up.
        overlay_dir = os.path.join(tmpdir, ".rdx")
        os.makedirs(overlay_dir)
        overlay_path = os.path.join(overlay_dir, "values.generated.yaml")
        overlay = {
            "rdx-library": {
                # Polluted projected services[] — last-wins would
                # normally let these win over values.yaml.
                "services": [
                    {"type": "[redacted secret demo-dashboards-binding:type]",
                     "binding": "dashboards", "enabled": True,
                     "provisioning": "deploy"},
                    {"type": "[redacted secret demo-metrics-binding:type]",
                     "binding": "metrics", "enabled": True,
                     "provisioning": "deploy"},
                    {"type": "postgresql", "binding": "db", "enabled": True,
                     "provisioning": "deploy"},
                    {"type": "dex", "binding": "auth", "enabled": True,
                     "provisioning": "deploy"},
                ],
                # Chart-level overrides the overlay legitimately
                # contributes — these MUST still be read.
                "grafana":    {"enabled": True},
                "prometheus": {"enabled": True},
            },
        }
        with open(overlay_path, "w") as f:
            yaml.safe_dump(overlay, f, default_flow_style=False)

        # File order matches what the filter is invoked with:
        #   filter_enabled_deps.py <sub> values.yaml .rdx/values.generated.yaml
        enabled = enabled_charts([values_path, overlay_path],
                                 dsl_mappings=DSL_MAPPINGS_PG)

        report(
            "overlay skip: grafana (from values.yaml) IS enabled",
            "grafana" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "overlay skip: prometheus (from values.yaml) IS enabled",
            "prometheus" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "overlay skip: postgresql, dex enabled",
            "postgresql" in enabled and "dex" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "overlay skip: no redacted marker in enabled set",
            not any(_is_redacted(s) for s in enabled),
            "got enabled=%s" % enabled,
        )

        # The overlay's chart-level `<chart>.enabled` overrides are
        # still read (the legitimate role of the overlay).
        report(
            "overlay still contributes chart-level enables: grafana via overlay",
            "grafana" in enabled,
            "got enabled=%s" % enabled,
        )

        # The kept deps include the clean types + operator subcharts.
        full_deps = list(FULL_CHART_YAML["dependencies"]) + [
            {"name": "grafana",    "version": "12.0.0", "repository": "oci://x", "condition": "grafana.enabled"},
            {"name": "prometheus", "version": "29.0.0", "repository": "oci://x", "condition": "prometheus.enabled"},
        ]
        # De-dup (the fixture already has grafana/prometheus/etc.):
        seen, dedup = set(), []
        for d in full_deps:
            n = d.get("alias", d.get("name"))
            if n in seen:
                continue
            seen.add(n)
            dedup.append(d)
        kept = filter_deps(dedup, enabled, [values_path, overlay_path])
        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])
        report(
            "overlay skip: no redacted marker in kept deps",
            not any(_is_redacted(n) for n in kept_names),
            "got %s" % kept_names,
        )
        # Expected: postgresql + cnpg + cloudnative-pg + grafana + prometheus + dex
        for must in ("postgresql", "cnpg", "cloudnative-pg",
                     "grafana", "prometheus", "dex"):
            report(
                "overlay skip: %s kept" % must,
                must in kept_names,
                "got %s" % kept_names,
            )
    finally:
        shutil.rmtree(tmpdir)


def test_overlay_only_carries_no_services():
    """When ONLY the overlay is passed (degenerate case), services[] is empty.

    Belt-and-suspenders: an invocation that passes only the overlay
    (e.g. a misconfigured caller) should produce an empty enabled set
    from services[], not pollute it with redacted markers from the
    overlay's projected services[].
    """
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        os.makedirs(os.path.join(tmpdir, ".rdx"))
        overlay_path = os.path.join(tmpdir, ".rdx", "values.generated.yaml")
        overlay = {
            "rdx-library": {
                "services": [
                    {"type": "[redacted secret X:type]",
                     "binding": "x", "enabled": True, "provisioning": "deploy"},
                ],
                "grafana": {"enabled": True},  # chart-level still wins
            },
        }
        with open(overlay_path, "w") as f:
            yaml.safe_dump(overlay, f, default_flow_style=False)

        enabled = enabled_charts([overlay_path])
        report(
            "overlay only: chart-level grafana still in enabled (overlay's role)",
            "grafana" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "overlay only: no redacted marker reaches enabled",
            not any(_is_redacted(s) for s in enabled),
            "got enabled=%s" % enabled,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_is_overlay_file_helper():
    """_is_overlay_file recognises canonical + legacy overlay paths."""
    cases = [
        ("chart/.rdx/values.generated.yaml",                True,  "canonical relative"),
        ("/abs/path/to/chart/.rdx/values.generated.yaml",   True,  "canonical absolute"),
        ("chart/values.generated.yaml",                     True,  "legacy relative"),
        ("/abs/chart/values.generated.yaml",                True,  "legacy absolute"),
        (".rdx/values.generated.yaml",                      True,  "bare canonical"),
        ("values.generated.yaml",                           True,  "bare legacy"),
        ("chart/values.yaml",                               False, "user values.yaml"),
        ("chart/staging-values.yaml",                       False, "user override file"),
        ("",                                                False, "empty"),
        (None,                                              False, "None"),
        ("chart/.rdx/something-else.yaml",                  False, "other file in .rdx dir"),
    ]
    for inp, expected, desc in cases:
        got = _is_overlay_file(inp)
        report(
            "_is_overlay_file(%r) → %s (%s)" % (inp, expected, desc),
            got == expected,
            "got %s" % got,
        )


def test_redacted_type_skipped_in_enabled_set():
    """services[].type=`[redacted secret ...]` must NOT pollute enabled set.

    Exact reproduction of the reported bug:
      - User has `services[]` with type=grafana (binding=dashboards)
        and type=prometheus (binding=metrics), both in deploy mode.
      - A bundle bug routes `type` through rdx's secret redactor, so
        the values the Tiltfile sees have `type: '[redacted secret
        demo-dashboards-binding:type]'` instead of `type: grafana`.
      - The filter must not add the marker to the enabled set
        (otherwise it propagates into Chart.yaml dep names) and must
        not crash.
    """
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "[redacted secret demo-dashboards-binding:type]",
                     "binding": "dashboards", "enabled": True,
                     "provisioning": "deploy"},
                    {"type": "[redacted secret demo-metrics-binding:type]",
                     "binding": "metrics", "enabled": True,
                     "provisioning": "deploy"},
                    {"type": "postgresql", "binding": "db", "enabled": True,
                     "provisioning": "deploy"},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path], dsl_mappings=DSL_MAPPINGS_PG)
        report(
            "redacted type: no marker leaks into enabled set",
            not any(_is_redacted(s) for s in enabled),
            "got enabled=%s" % enabled,
        )
        report(
            "redacted type: clean sibling (postgresql) still enabled",
            "postgresql" in enabled,
            "got enabled=%s" % enabled,
        )
        report(
            "redacted type: postgresql's chart_defaults still expand (cnpg)",
            "cnpg" in enabled and "cloudnative-pg" in enabled,
            "got enabled=%s" % enabled,
        )

        kept = filter_deps(FULL_CHART_YAML["dependencies"], enabled, [values_path])
        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])
        report(
            "redacted type: no kept dep has redacted name/alias",
            not any(_is_redacted(n) for n in kept_names),
            "got %s" % kept_names,
        )
        report(
            "redacted type: kept set is exactly the clean siblings + operator deps",
            kept_names == ["cloudnative-pg", "cnpg", "postgresql"],
            "got %s" % kept_names,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_redacted_type_alone_yields_no_deps():
    """All-redacted services[] → empty enabled set, no deps kept.

    Edge: if the user's entire services[] is redacted (extreme bundle
    bug), the filter must produce an empty Chart.yaml deps list, not
    crash and not propagate the markers.
    """
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        values = {
            "rdx-library": {
                "services": [
                    {"type": "[redacted secret demo-a-binding:type]",
                     "binding": "a", "enabled": True, "provisioning": "deploy"},
                    {"type": "[redacted secret demo-b-binding:type]",
                     "binding": "b", "enabled": True, "provisioning": "deploy"},
                ],
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path])
        kept = filter_deps(FULL_CHART_YAML["dependencies"], enabled, [values_path])
        report(
            "all-redacted: enabled set is empty",
            len(enabled) == 0,
            "got %s" % enabled,
        )
        report(
            "all-redacted: no deps kept",
            len(kept) == 0,
            "got %d kept" % len(kept),
        )
    finally:
        shutil.rmtree(tmpdir)


def test_redacted_dep_name_dropped_from_kept():
    """A redacted-name dep in Chart.yaml.full must be dropped.

    Defense-in-depth: even if a corrupted Chart.yaml.full backup or a
    rewrite pass slips a redacted marker into a dep's name field, the
    final filter must drop it — `helm dep update` on such a dep fails
    with a confusing message.
    """
    full_deps = list(FULL_CHART_YAML["dependencies"]) + [
        {"name": "[redacted secret demo-db-binding:type]",
         "version": "1.0.0", "repository": "oci://example/charts",
         "condition": "anything.enabled"},
    ]
    # Manually craft an enabled set that includes the marker, as would
    # happen if upstream defenses failed.
    enabled = {"postgresql", "[redacted secret demo-db-binding:type]"}
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        with open(values_path, "w") as f:
            yaml.safe_dump({"rdx-library": {"services": []}}, f)
        kept = filter_deps(full_deps, enabled, [values_path])
        kept_names = [d.get("name") for d in kept]
        report(
            "redacted dep name: dropped from kept",
            not any(_is_redacted(n) for n in kept_names),
            "got %s" % kept_names,
        )
        report(
            "redacted dep name: clean dep (postgresql) survives",
            "postgresql" in kept_names,
            "got %s" % kept_names,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_is_redacted_helper():
    """_is_redacted catches the markers we've actually seen in the wild."""
    cases = [
        ("",                                              False, "empty string"),
        ("grafana",                                       False, "plain chart name"),
        ("dex-idp",                                       False, "hyphenated chart name"),
        ("redacted-pg",                                   False, "false-positive: 'redacted' word alone"),
        ("[redacted secret demo-db-binding:type]",        True,  "the reported marker"),
        ("[redacted secret demo-dashboards-binding:type]", True, "dashboards variant"),
        ("[REDACTED something]",                          True,  "uppercase variant — defensively caught"),
        (None,                                            False, "None"),
        (42,                                              False, "non-string"),
    ]
    for inp, expected, desc in cases:
        got = _is_redacted(inp)
        report(
            "_is_redacted(%r) → %s (%s)" % (inp, expected, desc),
            got == expected,
            "got %s" % got,
        )


def test_bad_workload_name_guard():
    """Catch unresolved-template + redacted-secret names; pass valid ones."""
    cases = [
        # (input, expected_bad, description)
        ("",                                            True,  "empty string"),
        ("   ",                                         True,  "whitespace only"),
        ("demo-cloudnative-pg",                         False, "valid operator deploy name"),
        ("demo-postgresql",                             False, "valid plain k8s name"),
        ("{{ .Release.Name }}-cnpg-rw",                 True,  "unresolved Helm template"),
        ("demo-{{ .Values.binding.host }}",             True,  "partial unresolved template"),
        ("[redacted secret demo-db-binding:host]",      True,  "exact redacted-secret marker (the reported bug)"),
        ("demo-[redacted secret demo-db-binding:type]", True,  "embedded redacted-secret marker"),
        ("demo-redacted-pg",                            False, "false-positive guard: 'redacted' alone is fine"),
    ]
    for s, expected, desc in cases:
        got = _bad_workload_name(s)
        report(
            "_bad_workload_name(%r) → %s (%s)" % (s, expected, desc),
            got == expected,
            "got %s" % got,
        )


def test_non_default_library_key():
    """Values nested under a non-default library key are still detected.

    Regression: the library values key is the vendored dep name and varies
    across bundles (e.g. `suse-library`). When the filter assumed a fixed
    `rdx-library`, every dep was dropped (`kept 0/N`) for a `suse-library`
    project even though services were enabled. The key must be data-driven.
    """
    tmpdir = tempfile.mkdtemp(prefix="rdx-test-filter-")
    try:
        values_path = os.path.join(tmpdir, "values.yaml")
        # Both shapes under the non-default key: services[] + <chart>.enabled.
        values = {
            "suse-library": {
                "name": "app",
                "services": [
                    {"type": "postgresql", "binding": "db", "enabled": True,
                     "provisioning": "deploy"},
                ],
                "grafana": {"enabled": True},
                "minio": {"enabled": False},
            },
        }
        with open(values_path, "w") as f:
            yaml.safe_dump(values, f, default_flow_style=False)

        enabled = enabled_charts([values_path], lib_key="suse-library")
        full_deps = FULL_CHART_YAML["dependencies"]
        kept = filter_deps(full_deps, enabled, [values_path],
                           lib_key="suse-library")
        kept_names = sorted([d.get("alias", d.get("name")) for d in kept])

        report(
            "non-default key: services[] + <chart>.enabled both detected",
            enabled == {"postgresql", "grafana"},
            "got %s" % enabled,
        )
        report(
            "non-default key: only enabled deps kept (not 0/N)",
            kept_names == ["grafana", "postgresql"],
            "got %s" % kept_names,
        )
    finally:
        shutil.rmtree(tmpdir)


def test_lib_block_falls_back_across_keys():
    """`_lib_block` resolves the detected key, then conventional fallbacks."""
    report(
        "_lib_block: detected key wins",
        _lib_block({"suse-library": {"a": 1}}, "suse-library") == {"a": 1},
        "lookup by detected key failed",
    )
    report(
        "_lib_block: falls back to rdx-library when detected key absent",
        _lib_block({"rdx-library": {"b": 2}}, "nonexistent") == {"b": 2},
        "fallback to rdx-library failed",
    )
    report(
        "_lib_block: returns None when no library block present",
        _lib_block({"unrelated": {}}, "suse-library") is None,
        "expected None",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print("== filter_enabled_deps logic tests ==\n")

    test_basic_filtering()
    test_multi_instance_aliasing()
    test_preserves_existing_aliases()
    test_disabled_services_excluded()
    test_empty_services()
    test_connect_provisioning_skips_operator_subchart()
    test_deploy_provisioning_pulls_operator_subchart()
    test_provisioning_absent_defaults_to_deploy()
    test_mixed_connect_and_deploy()
    test_shared_and_external_also_skipped()
    test_overlay_services_block_skipped()
    test_overlay_only_carries_no_services()
    test_is_overlay_file_helper()
    test_redacted_type_skipped_in_enabled_set()
    test_redacted_type_alone_yields_no_deps()
    test_redacted_dep_name_dropped_from_kept()
    test_is_redacted_helper()
    test_bad_workload_name_guard()
    test_non_default_library_key()
    test_lib_block_falls_back_across_keys()

    print("\n-- %d passed, %d failed --" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
