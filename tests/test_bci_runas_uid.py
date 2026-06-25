#!/usr/bin/env python3
"""
Test the BCI-builder live-update runAsUser fix.

Root cause (verified locally with `docker inspect` + a real `pack build`):
the BCI builder builds as CNB_USER_ID=1001 but its run image launches the
container as uid 1002. The CNB lifecycle exports /workspace owned by the
BUILD user (1001) with 0755 dirs / 0644 files, so the LAUNCH user (1002)
cannot overwrite files under /workspace/src. Tilt's live_update sync runs
as the pod user, so its copy fails with EACCES, the synced file is never
replaced, and nodemon never reloads. The heroku builder uses one uid for
build+launch, so its sync works — which is why the bug is BCI-only.

Fix: for builder='bci', pin the app pod's runAsUser/runAsGroup to the
builder's BUILD uid/gid via the library subchart's podSecurityContext, so
the launched container owns the files live_update overwrites. Confirmed:
running the built image with `--user 1001:1000` makes the in-container
overwrite succeed and nodemon logs "restarting due to changes...".

The helm --set construction is built in the Tiltfile (Starlark) and
cannot be imported, so this test reimplements it exactly and asserts:

  1. builder='bci' -> emits <lib_key>.podSecurityContext.runAsUser=1001
     and .runAsGroup=1000.
  2. builder='heroku' -> emits NEITHER (heroku's build==launch uid).
  3. RDX_BCI_RUNAS_UID / RDX_BCI_RUNAS_GID override the defaults.
  4. RDX_BCI_RUNAS_UID=-1 disables the override entirely.
  5. The lib_key is honoured (bundles vendor the library under different
     names: rdx-library, suse-library, ...).

Usage: python3 test_bci_runas_uid.py
"""
import sys

# Mirror of the Tiltfile constants (rdx/Tiltfile, near BCI_BUILDER).
BCI_RUNAS_UID = 1001
BCI_RUNAS_GID = 1000


def build_helm_set(builder_kind, lib_key, env=None):
    """Mirror of the Tiltfile helm_set construction for the BCI uid fix.

    Keep this byte-for-byte aligned with rdx/Tiltfile's helm_set block.
    `env` stands in for os.getenv lookups.
    """
    env = env or {}
    helm_set = []
    if builder_kind == 'bci':
        bci_uid = int(env.get('RDX_BCI_RUNAS_UID', str(BCI_RUNAS_UID)))
        bci_gid = int(env.get('RDX_BCI_RUNAS_GID', str(BCI_RUNAS_GID)))
        if bci_uid >= 0:
            helm_set.append('%s.podSecurityContext.runAsUser=%d' % (lib_key, bci_uid))
            helm_set.append('%s.podSecurityContext.runAsGroup=%d' % (lib_key, bci_gid))
    return helm_set


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_bci_sets_runas_to_build_uid():
    s = build_helm_set('bci', 'rdx-library')
    _check('rdx-library.podSecurityContext.runAsUser=1001' in s,
           'bci must pin runAsUser to the build uid 1001: ' + repr(s))
    _check('rdx-library.podSecurityContext.runAsGroup=1000' in s,
           'bci must pin runAsGroup to the build gid 1000: ' + repr(s))


def test_heroku_sets_nothing():
    s = build_helm_set('heroku', 'rdx-library')
    _check(s == [],
           'heroku (single build/launch uid) must NOT pin runAsUser: ' + repr(s))


def test_helm_only_sets_nothing():
    # _builder_kind is None for helm_only / image_ref deploys.
    s = build_helm_set(None, 'rdx-library')
    _check(s == [], 'no builder -> no podSecurityContext override: ' + repr(s))


def test_env_override():
    s = build_helm_set('bci', 'rdx-library',
                       env={'RDX_BCI_RUNAS_UID': '2000', 'RDX_BCI_RUNAS_GID': '2000'})
    _check('rdx-library.podSecurityContext.runAsUser=2000' in s, repr(s))
    _check('rdx-library.podSecurityContext.runAsGroup=2000' in s, repr(s))
    _check('runAsUser=1001' not in ' '.join(s), 'default uid must not leak: ' + repr(s))


def test_disable_with_negative_uid():
    s = build_helm_set('bci', 'rdx-library', env={'RDX_BCI_RUNAS_UID': '-1'})
    _check(s == [], 'RDX_BCI_RUNAS_UID=-1 must disable the override: ' + repr(s))


def test_lib_key_is_honoured():
    # Bundles vendor the library under different names; the override must
    # nest under the detected key, not a hardcoded one.
    s = build_helm_set('bci', 'suse-library')
    _check('suse-library.podSecurityContext.runAsUser=1001' in s, repr(s))
    _check('rdx-library' not in ' '.join(s), 'must not hardcode rdx-library: ' + repr(s))


if __name__ == '__main__':
    failures = 0
    for fn in (test_bci_sets_runas_to_build_uid,
               test_heroku_sets_nothing,
               test_helm_only_sets_nothing,
               test_env_override,
               test_disable_with_negative_uid,
               test_lib_key_is_honoured):
        try:
            fn()
            sys.stdout.write('PASS %s\n' % fn.__name__)
        except AssertionError as e:
            failures += 1
            sys.stdout.write('FAIL %s: %s\n' % (fn.__name__, e))
    sys.exit(1 if failures else 0)
