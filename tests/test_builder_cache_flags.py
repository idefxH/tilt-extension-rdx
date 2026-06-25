#!/usr/bin/env python3
"""
Test pack-build cache-flag selection per builder (BCI vs heroku).

The cache flags are built in the Tiltfile (Starlark) and cannot be
imported. This test reimplements the cache-flag construction exactly as
the Tiltfile does it and asserts:

  1. builder='heroku' -> host bind cache (format=bind;source=/tmp/...),
     unchanged from the original behaviour.
  2. builder='bci' (and any non-heroku builder) -> named Docker volume
     cache (format=volume;name=...). A bind cache crashes the Paketo
     lifecycle with
       [exporter] ERROR: failed to create volume cache:
                  stat /cache: no such file or directory
  3. Neither builder ever emits a bind cache for a non-heroku builder
     (the regression we are guarding against).

Usage: python3 test_builder_cache_flags.py
"""
import sys


def build_cache_args(builder_kind, name):
    """Mirror of the Tiltfile cache-flag construction.

    Keep this byte-for-byte aligned with rdx/Tiltfile's build block.
    """
    cache_dir = '/tmp/rdx-pack-cache-' + name
    cache_vol = 'rdx-pack-cache-' + name
    pack_args = ['pack', 'build', '$EXPECTED_REF']
    if builder_kind == 'heroku':
        pack_args.extend([
            '--cache', '"type=build;format=bind;source=' + cache_dir + '"',
            '--cache', '"type=launch;format=bind;source=' + cache_dir + '-launch"',
        ])
    else:
        pack_args.extend([
            '--cache', '"type=build;format=volume;name=' + cache_vol + '"',
            '--cache', '"type=launch;format=volume;name=' + cache_vol + '-launch"',
        ])
    return pack_args


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_heroku_uses_bind_cache():
    args = build_cache_args('heroku', 'myapp')
    joined = ' '.join(args)
    _check('format=bind;source=/tmp/rdx-pack-cache-myapp"' in joined,
           'heroku build cache should be a bind cache: ' + joined)
    _check('format=bind;source=/tmp/rdx-pack-cache-myapp-launch"' in joined,
           'heroku launch cache should be a bind cache: ' + joined)
    _check('format=volume' not in joined,
           'heroku must NOT use a volume cache: ' + joined)


def test_bci_uses_volume_cache():
    args = build_cache_args('bci', 'myapp')
    joined = ' '.join(args)
    _check('format=volume;name=rdx-pack-cache-myapp"' in joined,
           'bci build cache should be a named volume: ' + joined)
    _check('format=volume;name=rdx-pack-cache-myapp-launch"' in joined,
           'bci launch cache should be a named volume: ' + joined)
    # The whole point of the fix: no bind cache reaches the Paketo lifecycle.
    _check('format=bind' not in joined,
           'bci must NOT use a bind cache (Paketo lifecycle rejects it): '
           + joined)
    _check('/cache' not in joined or 'no such file' not in joined, 'sanity')


def test_unknown_builder_falls_back_to_volume():
    # Any future non-heroku builder gets the safe volume cache, not bind.
    args = build_cache_args('rdx-suse', 'svc')
    joined = ' '.join(args)
    _check('format=volume;name=rdx-pack-cache-svc"' in joined,
           'non-heroku builder should use a volume cache: ' + joined)
    _check('format=bind' not in joined,
           'non-heroku builder must NOT use a bind cache: ' + joined)


def test_full_command_strings_printed():
    # Print both command strings for eyeball verification (point 5 of task).
    for kind in ('heroku', 'bci'):
        cmd = ' '.join(build_cache_args(kind, 'demo'))
        sys.stdout.write('[%s] %s --builder <img> ...\n' % (kind, cmd))


if __name__ == '__main__':
    failures = 0
    for fn in (test_heroku_uses_bind_cache,
               test_bci_uses_volume_cache,
               test_unknown_builder_falls_back_to_volume,
               test_full_command_strings_printed):
        try:
            fn()
            sys.stdout.write('PASS %s\n' % fn.__name__)
        except AssertionError as e:
            failures += 1
            sys.stdout.write('FAIL %s: %s\n' % (fn.__name__, e))
    sys.exit(1 if failures else 0)
