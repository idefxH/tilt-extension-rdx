#!/usr/bin/env python3
"""
Test the kube-preflight gate that guards load-time kubectl mutations.

_bootstrap_namespace() shells out to kubectl (namespace create,
pull-secret mirror) while the Tiltfile is being evaluated. With an
unreachable cluster those commands used to die mid-bootstrap with raw
kubectl noise (`connection refused`, `failed to download openapi`,
even an interactive auth prompt) and a traceback into the extension.
_kube_preflight() probes the active context once per Tiltfile
execution and turns that into a single fail() naming the kube context
and server URL.

The Starlark function can't be imported here, so this test:

  1. mirrors the session-once probe semantics (one probe per
     execution, all later calls skip);
  2. pins the Tiltfile wiring — guard key registered for the
     module-load reset, preflight called in _bootstrap_namespace
     BEFORE the first kubectl call, probe non-interactive and
     time-bounded, fail message carrying context + server.

Usage: python3 test_kube_preflight.py
"""
import os
import sys


def _expect(cond, msg):
    if not cond:
        sys.stderr.write("FAIL: " + msg + "\n")
        sys.exit(1)


_PREFIX = '_RDX_INTERNAL_ONCE_'


class _FakeEnv(object):
    """Mirror of the process-env session store (see the hairpin test)."""

    def __init__(self):
        self.vals = {}

    def getenv(self, key, default=''):
        return self.vals.get(key, default)

    def putenv(self, key, value):
        self.vals[key] = value

    def reset(self, key):
        self.putenv(_PREFIX + key, '')


def _session_once(env, key, identity):
    env_key = _PREFIX + key
    prev = env.getenv(env_key, '')
    if prev:
        return prev
    env.putenv(env_key, identity)
    return ''


def _kube_preflight(env, probe, failures):
    """Mirror of the guarded probe: first call probes, later calls skip;
    a dead probe records one failure (the Starlark fail())."""
    if _session_once(env, 'KUBE_PREFLIGHT', 'ok'):
        return
    if not probe():
        failures.append('unreachable')


def test_preflight_probes_once_per_session():
    env = _FakeEnv()
    env.reset('KUBE_PREFLIGHT')
    calls, failures = [], []
    probe = lambda: (calls.append(1), True)[1]

    # Three rdx_app() calls in one execution: one probe, no failures.
    for _ in range(3):
        _kube_preflight(env, probe, failures)
    _expect(len(calls) == 1, "healthy cluster must be probed exactly once")
    _expect(failures == [], "healthy cluster must not fail the load")

    # New execution (module-load reset): probes again.
    env.reset('KUBE_PREFLIGHT')
    _kube_preflight(env, probe, failures)
    _expect(len(calls) == 2, "each execution must re-probe after the reset")

    # Dead cluster: the first call fails the load.
    env2 = _FakeEnv()
    env2.reset('KUBE_PREFLIGHT')
    _kube_preflight(env2, lambda: False, failures)
    _expect(failures == ['unreachable'], "dead cluster must fail() once")
    print("ok  test_preflight_probes_once_per_session")


def test_tiltfile_preflight_wiring():
    """Pin the Tiltfile wiring a pure-python mirror can't execute."""
    tiltfile = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'rdx', 'Tiltfile')
    with open(tiltfile) as f:
        src = f.read()

    # Guard key registered for the module-load reset.
    keys_block = src.split('_SESSION_ONCE_KEYS = [', 1)[1].split(']', 1)[0]
    _expect("'KUBE_PREFLIGHT'" in keys_block,
            "KUBE_PREFLIGHT must be reset at module load")

    # Preflight runs in _bootstrap_namespace BEFORE the first kubectl
    # shell-out (the rdx probe / namespace create pipeline).
    body = src.split('def _bootstrap_namespace(', 1)[1].split('\ndef ', 1)[0]
    _expect('_kube_preflight()' in body,
            "_bootstrap_namespace must call the preflight")
    _expect(body.index('_kube_preflight()') < body.index('local('),
            "preflight must run before the first kubectl call")

    # Probe shape: bounded, non-interactive, no OpenAPI download needed.
    pf = src.split('def _kube_preflight(', 1)[1].split('\ndef ', 1)[0]
    _expect('kubectl version --request-timeout=' in pf,
            "probe must be a time-bounded kubectl version call")
    _expect('</dev/null' in pf,
            "probe must detach stdin so no kubeconfig can prompt")
    _expect("_session_once('KUBE_PREFLIGHT'" in pf,
            "probe must be a session-wide singleton")

    # The failure names the context and the server, and stays a fail()
    # (hard stop) rather than a warn().
    _expect('cannot reach the Kubernetes cluster' in pf and
            'k8s_context()' in pf and
            'cluster.server' in pf and
            'fail(' in pf,
            "failure must name context + server and hard-fail the load")
    print("ok  test_tiltfile_preflight_wiring")


if __name__ == '__main__':
    test_preflight_probes_once_per_session()
    test_tiltfile_preflight_wiring()
    print("\nAll kube-preflight tests passed.")
