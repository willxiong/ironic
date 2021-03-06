# -*- encoding: utf-8 -*-
# Copyright 2013 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import mock
from oslo_config import cfg
from oslo_utils import uuidutils
import pecan
from six.moves import http_client
from webob.static import FileIter
import wsme

from ironic.api.controllers.v1 import node as api_node
from ironic.api.controllers.v1 import utils
from ironic.common import exception
from ironic import objects
from ironic.tests import base
from ironic.tests.unit.api import utils as test_api_utils

CONF = cfg.CONF


class TestApiUtils(base.TestCase):

    def test_validate_limit(self):
        limit = utils.validate_limit(10)
        self.assertEqual(10, 10)

        # max limit
        limit = utils.validate_limit(999999999)
        self.assertEqual(CONF.api.max_limit, limit)

        # negative
        self.assertRaises(wsme.exc.ClientSideError, utils.validate_limit, -1)

        # zero
        self.assertRaises(wsme.exc.ClientSideError, utils.validate_limit, 0)

    def test_validate_sort_dir(self):
        sort_dir = utils.validate_sort_dir('asc')
        self.assertEqual('asc', sort_dir)

        # invalid sort_dir parameter
        self.assertRaises(wsme.exc.ClientSideError,
                          utils.validate_sort_dir,
                          'fake-sort')

    def test_get_patch_values_no_path(self):
        patch = [{'path': '/name', 'op': 'update', 'value': 'node-0'}]
        path = '/invalid'
        values = utils.get_patch_values(patch, path)
        self.assertEqual([], values)

    def test_get_patch_values_remove(self):
        patch = [{'path': '/name', 'op': 'remove'}]
        path = '/name'
        values = utils.get_patch_values(patch, path)
        self.assertEqual([], values)

    def test_get_patch_values_success(self):
        patch = [{'path': '/name', 'op': 'replace', 'value': 'node-x'}]
        path = '/name'
        values = utils.get_patch_values(patch, path)
        self.assertEqual(['node-x'], values)

    def test_get_patch_values_multiple_success(self):
        patch = [{'path': '/name', 'op': 'replace', 'value': 'node-x'},
                 {'path': '/name', 'op': 'replace', 'value': 'node-y'}]
        path = '/name'
        values = utils.get_patch_values(patch, path)
        self.assertEqual(['node-x', 'node-y'], values)

    def test_check_for_invalid_fields(self):
        requested = ['field_1', 'field_3']
        supported = ['field_1', 'field_2', 'field_3']
        utils.check_for_invalid_fields(requested, supported)

    def test_check_for_invalid_fields_fail(self):
        requested = ['field_1', 'field_4']
        supported = ['field_1', 'field_2', 'field_3']
        self.assertRaises(exception.InvalidParameterValue,
                          utils.check_for_invalid_fields,
                          requested, supported)

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_specify_fields(self, mock_request):
        mock_request.version.minor = 8
        self.assertIsNone(utils.check_allow_specify_fields(['foo']))

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_specify_fields_fail(self, mock_request):
        mock_request.version.minor = 7
        self.assertRaises(exception.NotAcceptable,
                          utils.check_allow_specify_fields, ['foo'])

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_specify_driver(self, mock_request):
        mock_request.version.minor = 16
        self.assertIsNone(utils.check_allow_specify_driver(['fake']))

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_specify_driver_fail(self, mock_request):
        mock_request.version.minor = 15
        self.assertRaises(exception.NotAcceptable,
                          utils.check_allow_specify_driver, ['fake'])

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_manage_verbs(self, mock_request):
        mock_request.version.minor = 4
        utils.check_allow_management_verbs('manage')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_manage_verbs_fail(self, mock_request):
        mock_request.version.minor = 3
        self.assertRaises(exception.NotAcceptable,
                          utils.check_allow_management_verbs, 'manage')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_provide_verbs(self, mock_request):
        mock_request.version.minor = 4
        utils.check_allow_management_verbs('provide')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_provide_verbs_fail(self, mock_request):
        mock_request.version.minor = 3
        self.assertRaises(exception.NotAcceptable,
                          utils.check_allow_management_verbs, 'provide')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_inspect_verbs(self, mock_request):
        mock_request.version.minor = 6
        utils.check_allow_management_verbs('inspect')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_inspect_verbs_fail(self, mock_request):
        mock_request.version.minor = 5
        self.assertRaises(exception.NotAcceptable,
                          utils.check_allow_management_verbs, 'inspect')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_abort_verbs(self, mock_request):
        mock_request.version.minor = 13
        utils.check_allow_management_verbs('abort')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_abort_verbs_fail(self, mock_request):
        mock_request.version.minor = 12
        self.assertRaises(exception.NotAcceptable,
                          utils.check_allow_management_verbs, 'abort')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_clean_verbs(self, mock_request):
        mock_request.version.minor = 15
        utils.check_allow_management_verbs('clean')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_clean_verbs_fail(self, mock_request):
        mock_request.version.minor = 14
        self.assertRaises(exception.NotAcceptable,
                          utils.check_allow_management_verbs, 'clean')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_check_allow_unknown_verbs(self, mock_request):
        utils.check_allow_management_verbs('rebuild')

    @mock.patch.object(pecan, 'request', spec_set=['version'])
    def test_allow_links_node_states_and_driver_properties(self, mock_request):
        mock_request.version.minor = 14
        self.assertTrue(utils.allow_links_node_states_and_driver_properties())
        mock_request.version.minor = 10
        self.assertFalse(utils.allow_links_node_states_and_driver_properties())


class TestNodeIdent(base.TestCase):

    def setUp(self):
        super(TestNodeIdent, self).setUp()
        self.valid_name = 'my-host'
        self.valid_uuid = uuidutils.generate_uuid()
        self.invalid_name = 'Mr Plow'
        self.node = test_api_utils.post_get_test_node()

    @mock.patch.object(pecan, 'request')
    def test_allow_node_logical_names_pre_name(self, mock_pecan_req):
        mock_pecan_req.version.minor = 1
        self.assertFalse(utils.allow_node_logical_names())

    @mock.patch.object(pecan, 'request')
    def test_allow_node_logical_names_post_name(self, mock_pecan_req):
        mock_pecan_req.version.minor = 5
        self.assertTrue(utils.allow_node_logical_names())

    @mock.patch("pecan.request")
    def test_is_valid_node_name(self, mock_pecan_req):
        mock_pecan_req.version.minor = 10
        self.assertTrue(utils.is_valid_node_name(self.valid_name))
        self.assertFalse(utils.is_valid_node_name(self.invalid_name))
        self.assertFalse(utils.is_valid_node_name(self.valid_uuid))

    @mock.patch.object(pecan, 'request')
    @mock.patch.object(utils, 'allow_node_logical_names')
    @mock.patch.object(objects.Node, 'get_by_uuid')
    @mock.patch.object(objects.Node, 'get_by_name')
    def test_get_rpc_node_expect_uuid(self, mock_gbn, mock_gbu, mock_anln,
                                      mock_pr):
        mock_anln.return_value = True
        self.node['uuid'] = self.valid_uuid
        mock_gbu.return_value = self.node
        self.assertEqual(self.node, utils.get_rpc_node(self.valid_uuid))
        self.assertEqual(1, mock_gbu.call_count)
        self.assertEqual(0, mock_gbn.call_count)

    @mock.patch.object(pecan, 'request')
    @mock.patch.object(utils, 'allow_node_logical_names')
    @mock.patch.object(objects.Node, 'get_by_uuid')
    @mock.patch.object(objects.Node, 'get_by_name')
    def test_get_rpc_node_expect_name(self, mock_gbn, mock_gbu, mock_anln,
                                      mock_pr):
        mock_pr.version.minor = 10
        mock_anln.return_value = True
        self.node['name'] = self.valid_name
        mock_gbn.return_value = self.node
        self.assertEqual(self.node, utils.get_rpc_node(self.valid_name))
        self.assertEqual(0, mock_gbu.call_count)
        self.assertEqual(1, mock_gbn.call_count)

    @mock.patch.object(pecan, 'request')
    @mock.patch.object(utils, 'allow_node_logical_names')
    @mock.patch.object(objects.Node, 'get_by_uuid')
    @mock.patch.object(objects.Node, 'get_by_name')
    def test_get_rpc_node_invalid_name(self, mock_gbn, mock_gbu,
                                       mock_anln, mock_pr):
        mock_pr.version.minor = 10
        mock_anln.return_value = True
        self.assertRaises(exception.InvalidUuidOrName,
                          utils.get_rpc_node,
                          self.invalid_name)

    @mock.patch.object(pecan, 'request')
    @mock.patch.object(utils, 'allow_node_logical_names')
    @mock.patch.object(objects.Node, 'get_by_uuid')
    @mock.patch.object(objects.Node, 'get_by_name')
    def test_get_rpc_node_by_uuid_no_logical_name(self, mock_gbn, mock_gbu,
                                                  mock_anln, mock_pr):
        # allow_node_logical_name() should have no effect
        mock_anln.return_value = False
        self.node['uuid'] = self.valid_uuid
        mock_gbu.return_value = self.node
        self.assertEqual(self.node, utils.get_rpc_node(self.valid_uuid))
        self.assertEqual(1, mock_gbu.call_count)
        self.assertEqual(0, mock_gbn.call_count)

    @mock.patch.object(pecan, 'request')
    @mock.patch.object(utils, 'allow_node_logical_names')
    @mock.patch.object(objects.Node, 'get_by_uuid')
    @mock.patch.object(objects.Node, 'get_by_name')
    def test_get_rpc_node_by_name_no_logical_name(self, mock_gbn, mock_gbu,
                                                  mock_anln, mock_pr):
        mock_anln.return_value = False
        self.node['name'] = self.valid_name
        mock_gbn.return_value = self.node
        self.assertRaises(exception.NodeNotFound,
                          utils.get_rpc_node,
                          self.valid_name)


class TestVendorPassthru(base.TestCase):

    def test_method_not_specified(self):
        self.assertRaises(wsme.exc.ClientSideError,
                          utils.vendor_passthru, 'fake-ident',
                          None, 'fake-topic', data='fake-data')

    @mock.patch.object(pecan, 'request',
                       spec_set=['method', 'context', 'rpcapi'])
    def _vendor_passthru(self, mock_request, async=True,
                         driver_passthru=False):
        return_value = {'return': 'SpongeBob', 'async': async, 'attach': False}
        mock_request.method = 'post'
        mock_request.context = 'fake-context'

        passthru_mock = None
        if driver_passthru:
            passthru_mock = mock_request.rpcapi.driver_vendor_passthru
        else:
            passthru_mock = mock_request.rpcapi.vendor_passthru
        passthru_mock.return_value = return_value

        response = utils.vendor_passthru('fake-ident', 'squarepants',
                                         'fake-topic', data='fake-data',
                                         driver_passthru=driver_passthru)

        passthru_mock.assert_called_once_with(
            'fake-context', 'fake-ident', 'squarepants', 'POST',
            'fake-data', 'fake-topic')
        self.assertIsInstance(response, wsme.api.Response)
        self.assertEqual('SpongeBob', response.obj)
        self.assertEqual(response.return_type, wsme.types.Unset)
        sc = http_client.ACCEPTED if async else http_client.OK
        self.assertEqual(sc, response.status_code)

    def test_vendor_passthru_async(self):
        self._vendor_passthru()

    def test_vendor_passthru_sync(self):
        self._vendor_passthru(async=False)

    def test_driver_vendor_passthru_async(self):
        self._vendor_passthru(driver_passthru=True)

    def test_driver_vendor_passthru_sync(self):
        self._vendor_passthru(async=False, driver_passthru=True)

    @mock.patch.object(pecan, 'response', spec_set=['app_iter'])
    @mock.patch.object(pecan, 'request',
                       spec_set=['method', 'context', 'rpcapi'])
    def _test_vendor_passthru_attach(self, return_value, expct_return_value,
                                     mock_request, mock_response):
        return_ = {'return': return_value, 'async': False, 'attach': True}
        mock_request.method = 'get'
        mock_request.context = 'fake-context'
        mock_request.rpcapi.driver_vendor_passthru.return_value = return_
        response = utils.vendor_passthru('fake-ident', 'bar',
                                         'fake-topic', data='fake-data',
                                         driver_passthru=True)
        mock_request.rpcapi.driver_vendor_passthru.assert_called_once_with(
            'fake-context', 'fake-ident', 'bar', 'GET',
            'fake-data', 'fake-topic')

        # Assert file was attached to the response object
        self.assertIsInstance(mock_response.app_iter, FileIter)
        self.assertEqual(expct_return_value,
                         mock_response.app_iter.file.read())
        # Assert response message is none
        self.assertIsInstance(response, wsme.api.Response)
        self.assertIsNone(response.obj)
        self.assertIsNone(response.return_type)
        self.assertEqual(http_client.OK, response.status_code)

    def test_vendor_passthru_attach(self):
        self._test_vendor_passthru_attach('foo', b'foo')

    def test_vendor_passthru_attach_unicode_to_byte(self):
        self._test_vendor_passthru_attach(u'não', b'n\xc3\xa3o')

    def test_vendor_passthru_attach_byte_to_byte(self):
        self._test_vendor_passthru_attach(b'\x00\x01', b'\x00\x01')

    def test_get_controller_reserved_names(self):
        expected = ['maintenance', 'management', 'ports', 'states',
                    'vendor_passthru', 'validate', 'detail']
        self.assertEqual(sorted(expected),
                         sorted(utils.get_controller_reserved_names(
                                api_node.NodesController)))
