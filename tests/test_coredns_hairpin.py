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

Usage: python3 test_coredns_hairpin.py
       chmod +x && ./test_coredns_hairpin.py
"""
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


if __name__ == '__main__':
    test_loopback_detection()
    test_configmap_shape()
    test_go_template_literal_preserved()
    test_slug_for_multilevel_domain()
    print("\nAll CoreDNS hairpin tests passed.")
