import os
import unittest
from unittest.mock import patch

from src.services import ebay_auth


class EbayAuthConfigTests(unittest.TestCase):

    def test_runame_value_does_not_emit_redirect_uri_warning(self):
        env = {
            'EBAY_APP_ID': 'app-id',
            'EBAY_CERT_ID': 'cert-id',
            'EBAY_REDIRECT_URI': 'xiaoting_pan-xiaoting-AquaVe-okccij',
        }
        with patch.dict(os.environ, env, clear=False), \
             patch.object(ebay_auth.EbayOAuthService, '_init_token_db', return_value=None), \
             patch('builtins.print') as mock_print:
            ebay_auth.EbayOAuthService('PRODUCTION')

        printed = '\n'.join(' '.join(map(str, call.args)) for call in mock_print.call_args_list)
        self.assertNotIn('Ensure this exact URI is registered', printed)

    def test_oauth_http_session_ignores_system_proxy(self):
        session = ebay_auth._create_oauth_session()

        self.assertFalse(session.trust_env)


if __name__ == '__main__':
    unittest.main()
