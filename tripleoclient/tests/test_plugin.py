# -*- coding: utf-8 -*-

# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from tripleoclient import plugin
from tripleoclient.tests import base
from tripleoclient.tests import fakes

import mock


class TestPlugin(base.TestCase):

    @mock.patch('ironicclient.client.get_client')
    def test_make_client(self, ironic_get_client):
        clientmgr = mock.MagicMock()
        clientmgr._api_version.__getitem__.return_value = '1'
        clientmgr.get_endpoint_for_service_type.return_value = fakes.AUTH_URL

        client = plugin.make_client(clientmgr)

        # The client should have a baremetal property. Accessing it should
        # fetch it from the clientmanager:
        baremetal = client.baremetal
        orchestration = client.orchestration
        # The second access should return the same clients:
        self.assertIs(client.baremetal, baremetal)
        self.assertIs(client.orchestration, orchestration)

        # And the functions should only be called per property:
        self.assertEqual(clientmgr.get_endpoint_for_service_type.call_count, 2)
        self.assertEqual(clientmgr.auth.get_token.call_count, 2)
