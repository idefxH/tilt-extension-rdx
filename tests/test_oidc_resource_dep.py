#!/usr/bin/env python3
"""
Test the app→OIDC/Dex startup-ordering wiring.

After `rdx workspace import` + `tilt up`, the app container connects to the
OIDC issuer (Dex) at boot. On a cold start Tilt could start the app pod
before Dex had a ready endpoint, so the app's first OIDC call failed and it
stayed unhealthy until a manual restart. The fix: the app workload(s) carry
a Tilt `resource_deps` on the registered Dex service resource, so Tilt brings
Dex up green before starting the app.

The wiring lives in Starlark (rdx/Tiltfile) and can't be imported. This test
reimplements `workload_name_for` + `_bad_workload_name` + the OIDC dep
collection byte-for-byte and asserts the dep list the app workloads receive.
Keep aligned with the "App → OIDC/Dex startup ordering" block in rdx/Tiltfile.

Usage: python3 test_oidc_resource_dep.py
"""
import sys

# A dsl-mappings-style catalog: dex's host template renders the Dex service.
DEX_CATALOG = {
    'dex': {
        'versions': [{
            'service': {'host': '{{ .Release.Name }}-dex.svc'},
        }],
    },
    # An operator-managed provider (hypothetical) to prove we skip it.
    'dex-operator': {
        'versions': [{
            'operator_managed': True,
            'operator_resource': '{{ .Release.Name }}-dex-operator',
        }],
    },
}

_OIDC_PROVIDER_TYPES = ['dex']


def workload_name_for(catalogued_charts, release, chart_alias, service_type):
    chart_entry = catalogued_charts.get(service_type, {})
    versions = chart_entry.get('versions', [])
    if not versions:
        return release + '-' + chart_alias
    ver = versions[0]
    if ver.get('operator_managed', False):
        op_tpl = ver.get('operator_resource', '')
        if op_tpl:
            if chart_alias != service_type:
                op_tpl = op_tpl.replace('-' + service_type, '-' + chart_alias, 1)
            return op_tpl.replace('{{ .Release.Name }}', release).replace('{{.Release.Name}}', release)
    host_tpl = ver.get('service', {}).get('host', '')
    if not host_tpl:
        return release + '-' + chart_alias
    if chart_alias != service_type:
        host_tpl = host_tpl.replace('-' + service_type + '.', '-' + chart_alias + '.', 1)
    rendered = host_tpl.replace('{{ .Release.Name }}', release).replace('{{.Release.Name}}', release)
    if '.' in rendered:
        rendered = rendered.split('.', 1)[0]
    return rendered


def _bad_workload_name(s):
    if not s or not s.strip():
        return True
    if '{{' in s or '}}' in s:
        return True
    if '[redacted' in s or 'redacted secret' in s:
        return True
    return False


def compute_oidc_deps(catalogued_charts, name, services):
    """Mirror of the OIDC/Dex dep-collection block in rdx_app()."""
    type_counts = {}
    for binding_name, service_type in services.items():
        type_counts[service_type] = type_counts.get(service_type, 0) + 1
    aliases = {}
    for binding_name, service_type in services.items():
        if type_counts[service_type] == 1:
            aliases[binding_name] = service_type
        else:
            aliases[binding_name] = service_type + '-' + binding_name

    deps = []
    for binding, stype in services.items():
        if stype not in _OIDC_PROVIDER_TYPES:
            continue
        ce = catalogued_charts.get(stype, {})
        cvs = ce.get('versions', [])
        if cvs and cvs[0].get('operator_managed', False):
            continue
        wl = workload_name_for(catalogued_charts, name, aliases[binding], stype)
        if _bad_workload_name(wl):
            continue
        if wl not in deps:
            deps.append(wl)
    return deps


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_app_depends_on_dex():
    deps = compute_oidc_deps(DEX_CATALOG, 'myapp', {'auth': 'dex'})
    _check(deps == ['myapp-dex'],
           'app should depend on the rendered Dex resource: ' + repr(deps))


def test_no_dex_means_no_deps():
    deps = compute_oidc_deps(DEX_CATALOG, 'myapp',
                             {'database': 'postgresql', 'cache': 'redis'})
    _check(deps == [],
           'no OIDC binding → no resource_deps added: ' + repr(deps))


def test_multi_instance_dex_aliased():
    # Two dex bindings → multi-instance aliasing (<type>-<binding>), and the
    # host template's type segment is rewritten to the alias.
    deps = compute_oidc_deps(DEX_CATALOG, 'myapp',
                             {'auth': 'dex', 'admin': 'dex'})
    _check(sorted(deps) == ['myapp-dex-admin', 'myapp-dex-auth'],
           'multi-instance dex should alias per binding: ' + repr(deps))


def test_dex_alongside_other_services():
    deps = compute_oidc_deps(DEX_CATALOG, 'myapp',
                             {'auth': 'dex', 'database': 'postgresql'})
    _check(deps == ['myapp-dex'],
           'only the dex service is added as a dep: ' + repr(deps))


def test_uncatalogued_dex_falls_back_to_legacy_name():
    # No catalog entry → legacy <release>-<type> name, still a valid dep.
    deps = compute_oidc_deps({}, 'myapp', {'auth': 'dex'})
    _check(deps == ['myapp-dex'],
           'uncatalogued dex falls back to <release>-dex: ' + repr(deps))


def test_operator_managed_provider_skipped():
    deps = compute_oidc_deps(DEX_CATALOG, 'myapp', {'auth': 'dex-operator'})
    # dex-operator is not an OIDC_PROVIDER_TYPE here, so nothing is added.
    _check(deps == [], 'non-listed provider type is ignored: ' + repr(deps))


def test_operator_managed_dex_type_skipped():
    # If 'dex' itself were operator-managed, we skip it (CR-name mismatch).
    cat = {'dex': {'versions': [{
        'operator_managed': True,
        'operator_resource': '{{ .Release.Name }}-dex-cr',
    }]}}
    deps = compute_oidc_deps(cat, 'myapp', {'auth': 'dex'})
    _check(deps == [],
           'operator-managed dex is not added as a host-name dep: ' + repr(deps))


def test_bad_name_skipped_no_dangling_dep():
    # An unresolved template token in the host → name is "bad" → skipped,
    # so we never emit a resource_deps entry that would abort the Tiltfile.
    cat = {'dex': {'versions': [{'service': {'host': '{{ .Values.x }}'}}]}}
    deps = compute_oidc_deps(cat, 'myapp', {'auth': 'dex'})
    _check(deps == [],
           'bad/unresolved dex name must be skipped: ' + repr(deps))


if __name__ == '__main__':
    failures = 0
    for fn in (test_app_depends_on_dex,
               test_no_dex_means_no_deps,
               test_multi_instance_dex_aliased,
               test_dex_alongside_other_services,
               test_uncatalogued_dex_falls_back_to_legacy_name,
               test_operator_managed_provider_skipped,
               test_operator_managed_dex_type_skipped,
               test_bad_name_skipped_no_dangling_dep):
        try:
            fn()
            sys.stdout.write('PASS %s\n' % fn.__name__)
        except AssertionError as e:
            failures += 1
            sys.stdout.write('FAIL %s: %s\n' % (fn.__name__, e))
    sys.exit(1 if failures else 0)
