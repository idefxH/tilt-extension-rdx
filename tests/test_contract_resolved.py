#!/usr/bin/env python3
"""Pin the contract-v1 resolved-block consumption (bundle SPEC
"VALUES-GENERATED CONTRACT": language_resolved / builder_resolved).

Two layers:
  * 1:1 mirrors of the pure Tiltfile helpers (_contract_language_block,
    _contract_builder_image) — Starlark can't be imported, keep them in
    lockstep with rdx/Tiltfile.
  * source-pinning: the image phase and the declared-build phase must
    keep PREFERRING the contract blocks while LANGUAGE_DEFAULTS and the
    legacy builder chain stay wired in as pre-contract fallbacks (FX:
    downgraded, not deleted).
"""
import os
import sys


# ── mirrors (keep byte-aligned with rdx/Tiltfile) ─────────────────────

def _contract_language_block(overlay, language):
    if not overlay or overlay.get('contract_version', 0) < 1:
        return None
    lr = overlay.get('language_resolved') or {}
    if lr.get('name') != language:
        return None
    return lr


def _contract_builder_image(overlay):
    if not overlay or overlay.get('contract_version', 0) < 1:
        return ''
    return (overlay.get('builder_resolved') or {}).get('image') or ''


def _contract_pack_block(overlay, language, effective_builder_image):
    if not overlay or overlay.get('contract_version', 0) < 1:
        return None
    pr = overlay.get('pack_resolved')
    if not pr:
        return None
    lr = overlay.get('language_resolved') or {}
    if lr.get('name') != language:
        return None
    br = (overlay.get('builder_resolved') or {}).get('image') or ''
    if br != effective_builder_image:
        return None
    return pr


_OVERLAY = {
    'contract_version': 1,
    'language_resolved': {
        'name': 'nodejs',
        'live_update_paths': ['src'],
        'install_trigger': ['package.json', 'package-lock.json'],
        'install_cmd': 'cd /workspace && npm install --production=false',
        'run_command': ['/cnb/process/dev'],
    },
    'builder_resolved': {
        'image': 'ghcr.io/idefxh/builder-bci-base:16.0.2',
        'source': 'config_default',
    },
    'pack_resolved': {
        'buildpacks': [],
        'env': {'NODE_ENV': 'development', 'BP_NODE_RUN_SCRIPTS': ''},
        'default_process': 'dev',
        'cache_format': 'volume',
        'preflights': [],
    },
    'rdx-library': {},
}


def _expect(cond, msg):
    if not cond:
        sys.stderr.write("FAIL: " + msg + "\n")
        sys.exit(1)


def test_language_block_gates():
    lr = _contract_language_block(_OVERLAY, 'nodejs')
    _expect(lr is not None and lr['install_trigger'][1] == 'package-lock.json',
            'matching language must return the block')
    _expect(_contract_language_block(_OVERLAY, 'python') is None,
            'name mismatch (stale overlay) must fall back')
    _expect(_contract_language_block({}, 'nodejs') is None,
            'missing overlay must fall back')
    pre = dict(_OVERLAY)
    pre.pop('contract_version')
    _expect(_contract_language_block(pre, 'nodejs') is None,
            'pre-contract overlay (no version key) must fall back')
    print('ok  test_language_block_gates')


def test_builder_image_gates():
    _expect(_contract_builder_image(_OVERLAY) ==
            'ghcr.io/idefxh/builder-bci-base:16.0.2',
            'contract builder image must surface')
    _expect(_contract_builder_image({}) == '',
            'missing overlay yields empty (fallback chain)')
    _expect(_contract_builder_image({'contract_version': 1}) == '',
            'missing block yields empty (fallback chain)')
    print('ok  test_builder_image_gates')


def test_pack_block_gates():
    # Matching (language, builder) pair: consumed.
    pr = _contract_pack_block(_OVERLAY, 'nodejs',
                              'ghcr.io/idefxh/builder-bci-base:16.0.2')
    _expect(pr is not None and pr['cache_format'] == 'volume',
            'matching pair must return the pack block')
    # Coupling rule: a different effective builder (explicit override,
    # fresher values) must fall back wholesale.
    _expect(_contract_pack_block(_OVERLAY, 'nodejs', 'heroku/builder:24')
            is None, 'builder mismatch must fall back')
    # Language mismatch (stale overlay) must fall back.
    _expect(_contract_pack_block(_OVERLAY, 'python',
                                 'ghcr.io/idefxh/builder-bci-base:16.0.2')
            is None, 'language mismatch must fall back')
    _expect(_contract_pack_block({}, 'nodejs', 'x') is None,
            'missing overlay must fall back')
    print('ok  test_pack_block_gates')


def test_mirrors_match_tiltfile_source():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'rdx', 'Tiltfile')).read()
    for fn_name in ('_contract_language_block', '_contract_builder_image',
                    '_contract_pack_block', '_builder_kind_for_image'):
        _expect('def %s(' % fn_name in src,
                'Tiltfile lost helper %s' % fn_name)
    print('ok  test_mirrors_match_tiltfile_source')


def test_image_phase_consumes_contract_with_fallback():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'rdx', 'Tiltfile')).read()
    body = src.split('def _resolve_image_strategy(', 1)[1].split('\ndef ', 1)[0]
    _expect('_contract_language_block(' in body,
            'image phase must read language_resolved')
    _expect('_contract_builder_image(' in body,
            'image phase must read builder_resolved')
    _expect('_contract_pack_block(' in body,
            'image phase must read pack_resolved')
    # Fallbacks stay wired (downgraded, not deleted).
    _expect("defaults['live_update_paths']" in body,
            'LANGUAGE_DEFAULTS sync paths must remain the fallback')
    _expect("defaults.get('live_update_install_cmd')" in body,
            'LANGUAGE_DEFAULTS install cmd must remain the fallback')
    _expect('_RDX_DEFAULT_BUILDER' in body,
            'runtime config chain must remain the fallback')
    _expect("defaults['buildpacks'] + extra_buildpacks" in body,
            'LANGUAGE_DEFAULTS buildpacks must remain the fallback')
    _expect("defaults.get('pack_build_env')" in body,
            'LANGUAGE_DEFAULTS pack env must remain the fallback')
    _expect('BUILDER_BUILD_ENV.get(' in body,
            'BUILDER_BUILD_ENV must remain the fallback env layer')
    _expect("defaults.get('pack_default_process')" in body,
            'LANGUAGE_DEFAULTS default process must remain the fallback')
    _expect("language == 'go'" in body,
            'go preflight language rule must remain the fallback')
    _expect("'bind' if _builder_kind == 'heroku' else 'volume'" in body,
            'kind cache rule must remain the fallback')
    # Contract-first precedence for the pack pieces.
    _expect(body.index('_contract_pack_block(') <
            body.index("defaults['buildpacks']"),
            'pack block must be consulted before the buildpack fallback')
    # Precedence: explicit argument still wins over the contract.
    _expect(body.index('live_update_paths != None') <
            body.index('_lang_resolved.get'),
            'explicit live_update_paths must outrank the contract')
    _expect(body.index('_values_build') < body.index('_from_contract = False'),
            'live values build.builder must outrank the contract tier')
    print('ok  test_image_phase_consumes_contract_with_fallback')


def test_declared_build_consumes_contract_with_fallback():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'rdx', 'Tiltfile')).read()
    body = src.split('def _register_declared_build(', 1)[1].split('\ndef ', 1)[0]
    _expect('_contract_builder_image(' in body,
            'declared-build phase must read builder_resolved')
    _expect("block.get('builder', '') or _RDX_DEFAULT_BUILDER or 'heroku'"
            in body,
            'pre-contract chain must remain the declared-build fallback')
    _expect(body.index('_contract_builder_image(') <
            body.index("block.get('builder', '')"),
            'contract must be consulted before the fallback chain')
    print('ok  test_declared_build_consumes_contract_with_fallback')


if __name__ == '__main__':
    test_language_block_gates()
    test_builder_image_gates()
    test_pack_block_gates()
    test_mirrors_match_tiltfile_source()
    test_image_phase_consumes_contract_with_fallback()
    test_declared_build_consumes_contract_with_fallback()
    print('\nAll contract-resolved tests passed.')
