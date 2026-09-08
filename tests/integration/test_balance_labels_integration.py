import copy
from unittest.mock import patch
import pytest
import agent_manager.core as core
import tests.integration.test_codex_labels_policy as label_cases


def test_relay_card_name_excludes_key_and_group_in_codex_configuration():
    case = label_cases.CodexLabelsPolicyTests()
    case.setUp()
    try:
        settings = case.settings
        settings['relayAccounts'] = [{'id':'relay-example','siteName':'示例中转'}]
        settings['providers'][0].update(relayAccountId='relay-example', name='示例中转 · 便宜线路 · Key 1')
        assert core._codex_gateway_provider_name(settings, None) == '示例中转'
        import tomllib
        for mode in ('aggregate','independent'):
            settings['modelWorkspace']['mode'] = mode
            doc = tomllib.loads(core.build_codex_config(settings))
            assert doc['model_providers'][doc['model_provider']]['name'] == '示例中转'
    finally:
        case.doCleanups()


@pytest.mark.parametrize('refresh_balance',[True,False])
def test_real_core_key_refresh_never_overwrites_dashboard_balance(refresh_balance):
    state = {'relayAccounts':[{'id':'relay_test','providerId':'p','balance':{'remaining':0},
              'balanceFreshness':'fresh','balanceUpdatedAt':'2026-09-09T00:00:00Z'}],
             'providers':[{'id':'p','models':['gpt-6-astra'],'balance':{'amount':999}}]}
    saved = []
    with patch.object(core,'load_settings',side_effect=lambda:copy.deepcopy(state)), \
         patch.object(core,'save_settings',side_effect=lambda s:saved.append(s)), \
         patch.object(core,'refresh_provider_models',return_value={'models':['gpt-6-astra']}), \
         patch.object(core,'fetch_provider_balance',return_value={'amount':999}), \
         patch.object(core,'_provider_runtime_requires_reapply',return_value=False):
        result=core.refresh_relay_account('relay_test',refresh_balance=refresh_balance)
    assert result['account']['balance']['remaining'] == 0
    assert result['account']['balanceUpdatedAt'] == '2026-09-09T00:00:00Z'
    assert saved[-1]['relayAccounts'][0]['balanceFreshness'] == 'fresh'
