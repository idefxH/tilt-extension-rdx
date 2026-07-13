#!/usr/bin/env python3
"""
Test the CoreDNS hairpin DNS fix logic.

The real helpers (`_domain_is_loopback`, `_coredns_hairpin_yaml`) are
embedded in the Tiltfile as Starlark — they cannot be imported directly.
This test reimplements their pure parts (the curated loopback-domain
match and the ConfigMap rendering) and validates:

  1. Loopback-domain detection: curated domains and their subdomains
     match; real domains do not.
  2. Generated ConfigMap is well-formed YAML with the expected name,
     namespace, managed-by label and workspace annotation.
  3. The CoreDNS `{{ .Name }}` Go-template token survives rendering
     verbatim (must NOT be interpolated by Starlark / our %-substitution).
  4. The embedded `.server` block scalar is a valid Corefile zone that
     carries the domain and the resolved ingress ClusterIP.
  5. Session-once guard: the ConfigMap has one fixed name, so in a
     multi-project workspace only the FIRST rdx_app() call registers it;
     identical later mappings no-op quietly, conflicting ones warn, and
     a new session (module reload) starts from a clean guard. Also pins
     the Tiltfile wiring: the guard sits between the module-level reset
     and the single k8s_yaml() registration site.

Usage: python3 test_coredns_hairpin.py
       chmod +x && ./test_coredns_hairpin.py
"""
import os
import sys

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML not installed; pip3 install pyyaml\n")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Reimplemented core logic (mirrors the Tiltfile-embedded helpers exactly).
# ---------------------------------------------------------------------------

_LOOPBACK_DOMAINS = [
    'localtest.me',
    'localhost.direct',
    'lvh.me',
    'vcap.me',
    'sslip.io',
    'nip.io',
]


def _domain_is_loopback_listmatch(domain):
    """Mirror of the curated-list fast path in the Tiltfile.

    The live-resolve slow path is not reimplemented here (it shells out);
    this covers the deterministic list match the gate relies on.
    """
    if not domain:
        return False
    for known in _LOOPBACK_DOMAINS:
        if domain == known or domain.endswith('.' + known):
            return True
    return False


def _coredns_hairpin_yaml(domain, ip, workspace):
    """Mirror of the Tiltfile-embedded renderer."""
    slug = domain.replace('.', '-')
    return """apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns-custom
  namespace: kube-system
  labels:
    app.kubernetes.io/managed-by: rdx
  annotations:
    rdx.io/workspace: "%s"
data:
  %s.server: |
    %s:53 {
        template IN A %s {
            answer "{{ .Name }} 60 IN A %s"
        }
    }
""" % (workspace, slug, domain, domain, ip)


_SESSION_ONCE_PREFIX = '_RDX_INTERNAL_ONCE_'


class _FakeEnv(object):
    """Stand-in for the Tilt process environment (os.getenv/os.putenv).

    Loaded-module globals are frozen in Starlark, so the Tiltfile keeps
    the guard state in the process env; the module-level reset clears it
    once per Tiltfile execution. `reset()` mirrors that module-load line.
    """

    def __init__(self):
        self.vals = {}

    def getenv(self, key, default=''):
        return self.vals.get(key, default)

    def putenv(self, key, value):
        self.vals[key] = value

    def reset(self, key):
        # Mirror of: os.putenv(_SESSION_ONCE_PREFIX + _once_key, '')
        self.putenv(_SESSION_ONCE_PREFIX + key, '')


def _session_once(env, key, identity):
    """Mirror of the Tiltfile-embedded `_session_once` guard."""
    env_key = _SESSION_ONCE_PREFIX + key
    prev = env.getenv(env_key, '')
    if prev:
        return prev
    env.putenv(env_key, identity)
    return ''


def _apply_hairpin_guarded(env, domain, ip, workspace, applied, warnings):
    """Mirror of the guarded tail of `_apply_coredns_hairpin`."""
    identity = '*.' + domain + ' -> ' + ip
    prev = _session_once(env, 'COREDNS_HAIRPIN', identity)
    if prev:
        if prev != identity:
            warnings.append(
                'coredns-custom already registered for %s; '
                'ignoring %s requested by %s' % (prev, identity, workspace))
        return
    applied.append(_coredns_hairpin_yaml(domain, ip, workspace))


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def _expect(cond, msg):
    if not cond:
        sys.stderr.write("FAIL: " + msg + "\n")
        sys.exit(1)


def test_loopback_detection():
    # Curated domains and their subdomains match.
    for d in _LOOPBACK_DOMAINS:
        _expect(_domain_is_loopback_listmatch(d),
                "bare loopback domain should match: " + d)
        _expect(_domain_is_loopback_listmatch('app.' + d),
                "subdomain of loopback domain should match: app." + d)
    # Real domains and the empty string do not.
    for d in ['', 'staging.corp.com', 'example.com', 'notlocaltest.me']:
        _expect(not _domain_is_loopback_listmatch(d),
                "non-loopback domain must NOT match: " + repr(d))
    print("ok  test_loopback_detection")


def test_configmap_shape():
    out = _coredns_hairpin_yaml('localtest.me', '10.43.12.34', 'my-workspace')
    doc = yaml.safe_load(out)

    _expect(doc['apiVersion'] == 'v1', "apiVersion")
    _expect(doc['kind'] == 'ConfigMap', "kind")
    _expect(doc['metadata']['name'] == 'coredns-custom',
            "ConfigMap name must be the k3s convention 'coredns-custom'")
    _expect(doc['metadata']['namespace'] == 'kube-system',
            "namespace must be kube-system")
    _expect(doc['metadata']['labels']['app.kubernetes.io/managed-by'] == 'rdx',
            "managed-by label")
    _expect(doc['metadata']['annotations']['rdx.io/workspace'] == 'my-workspace',
            "workspace annotation")

    # The data key is the dotted-to-dashed slug + .server
    _expect('localtest-me.server' in doc['data'],
            "data key should be '<slug>.server', got: " +
            repr(list(doc['data'].keys())))
    print("ok  test_configmap_shape")


def test_go_template_literal_preserved():
    out = _coredns_hairpin_yaml('localtest.me', '10.43.12.34', 'ws')
    doc = yaml.safe_load(out)
    zone = doc['data']['localtest-me.server']
    # The Go-template token must reach CoreDNS verbatim.
    _expect('{{ .Name }}' in zone,
            "CoreDNS '{{ .Name }}' token must be preserved verbatim; got:\n" +
            zone)
    # And the domain + resolved ingress IP must be present in the zone.
    _expect('localtest.me:53 {' in zone, "zone header for the domain")
    _expect('template IN A localtest.me {' in zone, "template directive")
    _expect('answer "{{ .Name }} 60 IN A 10.43.12.34"' in zone,
            "answer line must carry the ingress ClusterIP; got:\n" + zone)
    print("ok  test_go_template_literal_preserved")


def test_slug_for_multilevel_domain():
    out = _coredns_hairpin_yaml('dev.localtest.me', '10.0.0.1', 'ws')
    doc = yaml.safe_load(out)
    _expect('dev-localtest-me.server' in doc['data'],
            "every dot becomes a dash in the slug; got: " +
            repr(list(doc['data'].keys())))
    print("ok  test_slug_for_multilevel_domain")


def test_session_once_multi_project():
    """A multi-project workspace registers the ConfigMap exactly once."""
    env = _FakeEnv()
    env.reset('COREDNS_HAIRPIN')  # module-load reset
    applied, warnings = [], []

    # First project wins the claim and registers.
    _apply_hairpin_guarded(env, 'localtest.me', '10.43.0.1', 'grist',
                           applied, warnings)
    _expect(len(applied) == 1, "first rdx_app call must register the CM")
    _expect(len(warnings) == 0, "no warning on the winning call")

    # Second project, identical mapping: quiet no-op — this is the exact
    # shape that used to die with `Duplicate YAML: ConfigMap coredns-custom`.
    _apply_hairpin_guarded(env, 'localtest.me', '10.43.0.1', 'minio-setup',
                           applied, warnings)
    _expect(len(applied) == 1, "identical mapping must not re-register")
    _expect(len(warnings) == 0, "identical mapping must not warn")

    # Third project, conflicting mapping (different domain or ingress IP —
    # shouldn't happen inside one cluster, but must be visible, not fatal).
    _apply_hairpin_guarded(env, 'lvh.me', '10.43.0.1', 'other',
                           applied, warnings)
    _expect(len(applied) == 1, "conflicting mapping must not re-register")
    _expect(len(warnings) == 1, "conflicting mapping must warn")
    _expect('*.localtest.me -> 10.43.0.1' in warnings[0] and
            '*.lvh.me -> 10.43.0.1' in warnings[0],
            "warning must name both the registered and the ignored " +
            "mapping; got: " + warnings[0])

    # Winner's manifest is the one registered.
    doc = yaml.safe_load(applied[0])
    _expect('localtest-me.server' in doc['data'],
            "the first project's domain must be the registered zone")
    print("ok  test_session_once_multi_project")


def test_session_once_resets_per_session():
    """The module-load reset re-arms the guard on every Tiltfile
    execution, so reloads in a running `tilt up` re-register the CM
    (instead of dropping it because a stale env value survived in the
    Tilt process)."""
    env = _FakeEnv()
    env.reset('COREDNS_HAIRPIN')
    applied, warnings = [], []
    _apply_hairpin_guarded(env, 'localtest.me', '10.43.0.1', 'ws',
                           applied, warnings)
    _expect(len(applied) == 1, "session 1 registers")

    # New Tiltfile execution: module top level runs again in the same
    # process — the reset must clear the previous session's claim.
    env.reset('COREDNS_HAIRPIN')
    _apply_hairpin_guarded(env, 'localtest.me', '10.43.0.1', 'ws',
                           applied, warnings)
    _expect(len(applied) == 2, "session 2 must register again after reset")
    _expect(len(warnings) == 0, "no warnings across sessions")
    print("ok  test_session_once_resets_per_session")


def test_tiltfile_guard_wiring():
    """Pin the Tiltfile wiring of the guard (the parts a pure-python
    mirror can't execute): module-level reset, guard checked inside
    _apply_coredns_hairpin BEFORE the single k8s_yaml registration."""
    tiltfile = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'rdx', 'Tiltfile')
    with open(tiltfile) as f:
        src = f.read()

    _expect("os.putenv(_SESSION_ONCE_PREFIX + _once_key, '')" in src,
            "module-level guard reset must exist (once per execution)")
    _expect(src.count('k8s_yaml(blob(_coredns_hairpin_yaml(') == 1,
            "exactly one registration site for the hairpin ConfigMap")

    body = src.split('def _apply_coredns_hairpin(', 1)[1]
    body = body.split('\ndef ', 1)[0]
    _expect("_session_once('COREDNS_HAIRPIN'" in body,
            "_apply_coredns_hairpin must claim the session guard")
    _expect(body.index("_session_once('COREDNS_HAIRPIN'") <
            body.index('k8s_yaml(blob(_coredns_hairpin_yaml('),
            "guard must be checked before k8s_yaml registers the CM")
    print("ok  test_tiltfile_guard_wiring")


if __name__ == '__main__':
    test_loopback_detection()
    test_configmap_shape()
    test_go_template_literal_preserved()
    test_slug_for_multilevel_domain()
    test_session_once_multi_project()
    test_session_once_resets_per_session()
    test_tiltfile_guard_wiring()
    print("\nAll CoreDNS hairpin tests passed.")
