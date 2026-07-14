#!/usr/bin/env python3
"""Mechanical drift guard: the pre-contract fallback copies in this
extension (LANGUAGE_DEFAULTS core fields, BUILDER_BUILD_ENV, the
builder image pins) must match tests/canonical_language_table.json —
an exact copy of the rdx engine's canonical export
(pkg/engine/testdata/canonical-language-table.json).

The two JSON copies are themselves kept in lockstep by the
fallback-lockstep step in the rdx repo's CI, which diffs them at the
ext SHA pinned in tests/e2e/versions.yaml. So: engine table ↔ rdx JSON
(Go test) ↔ ext JSON (CI diff) ↔ LANGUAGE_DEFAULTS (this test) — a
drifted fallback fails a build somewhere, mechanically, with no
cross-repo network at unit-test time.

The Tiltfile is Starlark but its table literals are python-parseable:
ast.parse + literal_eval extracts them without executing anything.
"""
import ast
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _expect(cond, msg):
    if not cond:
        sys.stderr.write("FAIL: " + msg + "\n")
        sys.exit(1)


def _tiltfile_tree():
    src = open(os.path.join(_HERE, '..', 'rdx', 'Tiltfile')).read()
    return ast.parse(src)


def _assign(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError('Tiltfile: top-level assignment %s not found' % name)


def _canonical():
    with open(os.path.join(_HERE, 'canonical_language_table.json')) as f:
        return json.load(f)


def _norm_paths(paths):
    # The ext table writes './src'; the canonical writes 'src'. The
    # consumption code lstrips './' too — normalize the same way.
    return [p.lstrip('./') for p in (paths or [])]


def test_language_defaults_match_canonical():
    tree = _tiltfile_tree()
    defaults = _assign(tree, 'LANGUAGE_DEFAULTS')
    canon = _canonical()['languages']

    _expect(set(defaults.keys()) == set(canon.keys()),
            'language sets differ: ext=%s canonical=%s'
            % (sorted(defaults), sorted(canon)))

    for lang, row in sorted(canon.items()):
        ext = defaults[lang]
        _expect(_norm_paths(ext.get('live_update_paths')) ==
                row['live_update_paths'],
                '%s live_update_paths drifted: ext=%s canonical=%s'
                % (lang, ext.get('live_update_paths'),
                   row['live_update_paths']))
        _expect(_norm_paths(ext.get('live_update_install_trigger')) ==
                row['install_trigger'],
                '%s install_trigger drifted: ext=%s canonical=%s'
                % (lang, ext.get('live_update_install_trigger'),
                   row['install_trigger']))
        _expect((ext.get('live_update_install_cmd') or '') ==
                row['install_cmd'],
                '%s install_cmd drifted' % lang)
        _expect(list(ext.get('buildpacks') or []) == row['buildpacks'],
                '%s buildpacks drifted: ext=%s canonical=%s'
                % (lang, ext.get('buildpacks'), row['buildpacks']))
        _expect(dict(ext.get('pack_build_env') or {}) == row['pack_env'],
                '%s pack env drifted: ext=%s canonical=%s'
                % (lang, ext.get('pack_build_env'), row['pack_env']))
        _expect((ext.get('pack_default_process') or '') ==
                row['default_process'],
                '%s default_process drifted' % lang)
    print('ok  test_language_defaults_match_canonical')


def test_bci_env_layer_matches_canonical():
    tree = _tiltfile_tree()
    builder_env = _assign(tree, 'BUILDER_BUILD_ENV')
    canon = _canonical()['bci_env']
    _expect(dict(builder_env.get('bci') or {}) ==
            {k: dict(v) for k, v in canon.items()},
            'BUILDER_BUILD_ENV[bci] drifted from canonical bci_env: '
            'ext=%s canonical=%s' % (builder_env.get('bci'), canon))
    _expect(set(builder_env.keys()) <= {'bci'},
            'BUILDER_BUILD_ENV grew a builder the canonical does not '
            'know: %s — extend the engine table first' %
            sorted(builder_env))
    print('ok  test_bci_env_layer_matches_canonical')


def test_builder_pins_match_canonical():
    tree = _tiltfile_tree()
    pins = _canonical()['builder_pins']
    _expect(_assign(tree, 'DEFAULT_BUILDER') == pins['heroku'],
            'DEFAULT_BUILDER drifted from canonical builder_pins.heroku')
    _expect(_assign(tree, 'BCI_BUILDER') == pins['bci'],
            'BCI_BUILDER drifted from canonical builder_pins.bci')
    print('ok  test_builder_pins_match_canonical')


def test_cache_rule_matches_canonical():
    # The ext fallback rule is code ("'bind' if kind == 'heroku' else
    # 'volume'"); assert the canonical's resolved decisions agree with
    # it, so a rule change on either side trips this test.
    canon = _canonical()['cache_format']
    rule = lambda kind: 'bind' if kind == 'heroku' else 'volume'
    _expect(canon['heroku_family'] == rule('heroku'),
            'heroku cache decision drifted')
    _expect(canon['bci'] == rule('bci'), 'bci cache decision drifted')
    _expect(canon['unknown'] == rule('something-else'),
            'unknown-builder cache decision drifted')
    print('ok  test_cache_rule_matches_canonical')


def test_go_preflight_named_in_canonical():
    # The ext fallback keys the tidy preflight on language == 'go'
    # (code); the canonical names it as data. If either side changes,
    # this trips and a human reconciles.
    canon = _canonical()['languages']
    _expect(canon['go']['preflights'] == ['go-mod-tidy'],
            "canonical go preflights changed — update the ext's "
            "fallback gate (language == 'go') accordingly")
    for lang in ('nodejs', 'python', 'java'):
        _expect(canon[lang]['preflights'] == [],
                '%s gained a preflight the ext fallback does not '
                'implement' % lang)
    print('ok  test_go_preflight_named_in_canonical')


if __name__ == '__main__':
    test_language_defaults_match_canonical()
    test_bci_env_layer_matches_canonical()
    test_builder_pins_match_canonical()
    test_cache_rule_matches_canonical()
    test_go_preflight_named_in_canonical()
    print('\nAll fallback-lockstep tests passed.')
