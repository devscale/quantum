# Copyright (c) 2012 OpenStack Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from quantum.api.v2 import attributes as attr
from quantum.common.test_lib import test_config
from quantum import context
from quantum.db import db_base_plugin_v2
from quantum.db import portsecurity_db
from quantum.db import securitygroups_db
from quantum.extensions import portsecurity as psec
from quantum.extensions import securitygroup as ext_sg
from quantum.manager import QuantumManager
from quantum import policy
from quantum.tests.unit import test_db_plugin

DB_PLUGIN_KLASS = ('quantum.tests.unit.test_extension_portsecurity.'
                   'PortSecurityTestPlugin')


class PortSecurityTestCase(test_db_plugin.QuantumDbPluginV2TestCase):
    def setUp(self, plugin=None):
        super(PortSecurityTestCase, self).setUp()

        # Check if a plugin supports security groups
        plugin_obj = QuantumManager.get_plugin()
        self._skip_security_group = ('security-group' not in
                                     plugin_obj.supported_extension_aliases)

    def tearDown(self):
        super(PortSecurityTestCase, self).tearDown()
        self._skip_security_group = None


class PortSecurityTestPlugin(db_base_plugin_v2.QuantumDbPluginV2,
                             securitygroups_db.SecurityGroupDbMixin,
                             portsecurity_db.PortSecurityDbMixin):

    """Test plugin that implements necessary calls on create/delete port for
    associating ports with security groups and port security.
    """

    supported_extension_aliases = ["security-group", "port-security"]
    port_security_enabled_create = "create_port:port_security_enabled"
    port_security_enabled_update = "update_port:port_security_enabled"

    def _enforce_set_auth(self, context, resource, action):
        return policy.enforce(context, action, resource)

    def create_network(self, context, network):
        tenant_id = self._get_tenant_id_for_create(context, network['network'])
        self._ensure_default_security_group(context, tenant_id)
        with context.session.begin(subtransactions=True):
            quantum_db = super(PortSecurityTestPlugin, self).create_network(
                context, network)
            quantum_db.update(network['network'])
            self._process_network_create_port_security(
                context, quantum_db)
            self._extend_network_port_security_dict(context, quantum_db)
        return quantum_db

    def update_network(self, context, id, network):
        with context.session.begin(subtransactions=True):
            quantum_db = super(PortSecurityTestPlugin, self).update_network(
                context, id, network)
            if psec.PORTSECURITY in network['network']:
                self._update_network_security_binding(
                    context, id, network['network'][psec.PORTSECURITY])
            self._extend_network_port_security_dict(
                context, quantum_db)
        return quantum_db

    def get_network(self, context, id, fields=None):
        with context.session.begin(subtransactions=True):
            net = super(PortSecurityTestPlugin, self).get_network(
                context, id)
            self._extend_network_port_security_dict(context, net)
        return self._fields(net, fields)

    def create_port(self, context, port):
        if attr.is_attr_set(port['port'][psec.PORTSECURITY]):
            self._enforce_set_auth(context, port,
                                   self.port_security_enabled_create)
        p = port['port']
        with context.session.begin(subtransactions=True):
            p[ext_sg.SECURITYGROUPS] = self._get_security_groups_on_port(
                context, port)
            quantum_db = super(PortSecurityTestPlugin, self).create_port(
                context, port)
            p.update(quantum_db)

            (port_security, has_ip) = self._determine_port_security_and_has_ip(
                context, p)
            p[psec.PORTSECURITY] = port_security
            self._process_port_security_create(context, p)

            if (attr.is_attr_set(p.get(ext_sg.SECURITYGROUPS)) and
                not (port_security and has_ip)):
                raise psec.PortSecurityAndIPRequiredForSecurityGroups()

            # Port requires ip and port_security enabled for security group
            if has_ip and port_security:
                self._ensure_default_security_group_on_port(context, port)

            if (p.get(ext_sg.SECURITYGROUPS) and p[psec.PORTSECURITY]):
                self._process_port_create_security_group(
                    context, p['id'], p[ext_sg.SECURITYGROUPS])

            self._extend_port_dict_security_group(context, p)
            self._extend_port_port_security_dict(context, p)

        return port['port']

    def update_port(self, context, id, port):
        self._enforce_set_auth(context, port,
                               self.port_security_enabled_update)
        delete_security_groups = self._check_update_deletes_security_groups(
            port)
        has_security_groups = self._check_update_has_security_groups(port)
        with context.session.begin(subtransactions=True):
            ret_port = super(PortSecurityTestPlugin, self).update_port(
                context, id, port)
            # copy values over
            ret_port.update(port['port'])

            # populate port_security setting
            if psec.PORTSECURITY not in ret_port:
                ret_port[psec.PORTSECURITY] = self._get_port_security_binding(
                    context, id)
            has_ip = self._ip_on_port(ret_port)
            # checks if security groups were updated adding/modifying
            # security groups, port security is set and port has ip
            if (has_security_groups and (not ret_port[psec.PORTSECURITY]
                                         or not has_ip)):
                raise psec.PortSecurityAndIPRequiredForSecurityGroups()

            # Port security/IP was updated off. Need to check that no security
            # groups are on port.
            if (ret_port[psec.PORTSECURITY] is not True or not has_ip):
                if has_security_groups:
                    raise psec.PortSecurityAndIPRequiredForSecurityGroups()

                # get security groups on port
                filters = {'port_id': [id]}
                security_groups = (super(PortSecurityTestPlugin, self).
                                   _get_port_security_group_bindings(
                                   context, filters))
                if security_groups and not delete_security_groups:
                    raise psec.PortSecurityPortHasSecurityGroup()

            if (delete_security_groups or has_security_groups):
                # delete the port binding and read it with the new rules.
                self._delete_port_security_group_bindings(context, id)
                sgids = self._get_security_groups_on_port(context, port)
                self._process_port_create_security_group(context, id, sgids)

            if psec.PORTSECURITY in port['port']:
                self._update_port_security_binding(
                    context, id, ret_port[psec.PORTSECURITY])

            self._extend_port_port_security_dict(context, ret_port)
            self._extend_port_dict_security_group(context, ret_port)

        return ret_port


class PortSecurityDBTestCase(PortSecurityTestCase):
    def setUp(self, plugin=None):
        test_config['plugin_name_v2'] = DB_PLUGIN_KLASS
        super(PortSecurityDBTestCase, self).setUp()

    def tearDown(self):
        del test_config['plugin_name_v2']
        super(PortSecurityDBTestCase, self).tearDown()


class TestPortSecurity(PortSecurityDBTestCase):
    def test_create_network_with_portsecurity_mac(self):
        res = self._create_network('json', 'net1', True)
        net = self.deserialize('json', res)
        self.assertEqual(net['network'][psec.PORTSECURITY], True)

    def test_create_network_with_portsecurity_false(self):
        res = self._create_network('json', 'net1', True,
                                   arg_list=('port_security_enabled',),
                                   port_security_enabled=False)
        net = self.deserialize('json', res)
        self.assertEqual(net['network'][psec.PORTSECURITY], False)

    def test_updating_network_port_security(self):
        res = self._create_network('json', 'net1', True,
                                   port_security_enabled='True')
        net = self.deserialize('json', res)
        self.assertEqual(net['network'][psec.PORTSECURITY], True)
        update_net = {'network': {psec.PORTSECURITY: False}}
        req = self.new_update_request('networks', update_net,
                                      net['network']['id'])
        net = self.deserialize('json', req.get_response(self.api))
        self.assertEqual(net['network'][psec.PORTSECURITY], False)
        req = self.new_show_request('networks', net['network']['id'])
        net = self.deserialize('json', req.get_response(self.api))
        self.assertEqual(net['network'][psec.PORTSECURITY], False)

    def test_create_port_default_true(self):
        with self.network() as net:
            res = self._create_port('json', net['network']['id'])
            port = self.deserialize('json', res)
            self.assertEqual(port['port'][psec.PORTSECURITY], True)
            self._delete('ports', port['port']['id'])

    def test_create_port_passing_true(self):
        res = self._create_network('json', 'net1', True,
                                   arg_list=('port_security_enabled',),
                                   port_security_enabled=True)
        net = self.deserialize('json', res)
        res = self._create_port('json', net['network']['id'])
        port = self.deserialize('json', res)
        self.assertEqual(port['port'][psec.PORTSECURITY], True)
        self._delete('ports', port['port']['id'])

    def test_create_port_on_port_security_false_network(self):
        res = self._create_network('json', 'net1', True,
                                   arg_list=('port_security_enabled',),
                                   port_security_enabled=False)
        net = self.deserialize('json', res)
        res = self._create_port('json', net['network']['id'])
        port = self.deserialize('json', res)
        self.assertEqual(port['port'][psec.PORTSECURITY], False)
        self._delete('ports', port['port']['id'])

    def test_create_port_security_overrides_network_value(self):
        res = self._create_network('json', 'net1', True,
                                   arg_list=('port_security_enabled',),
                                   port_security_enabled=False)
        net = self.deserialize('json', res)
        res = self._create_port('json', net['network']['id'],
                                arg_list=('port_security_enabled',),
                                port_security_enabled=True)
        port = self.deserialize('json', res)
        self.assertEqual(port['port'][psec.PORTSECURITY], True)
        self._delete('ports', port['port']['id'])

    def test_create_port_with_default_security_group(self):
        if self._skip_security_group:
            self.skipTest("Plugin does not support security groups")
        with self.network() as net:
            with self.subnet(network=net):
                res = self._create_port('json', net['network']['id'])
                port = self.deserialize('json', res)
                self.assertEqual(port['port'][psec.PORTSECURITY], True)
                self.assertEqual(len(port['port'][ext_sg.SECURITYGROUPS]), 1)
                self._delete('ports', port['port']['id'])

    def test_update_port_security_off_with_security_group(self):
        if self._skip_security_group:
            self.skipTest("Plugin does not support security groups")
        with self.network() as net:
            with self.subnet(network=net):
                res = self._create_port('json', net['network']['id'])
                port = self.deserialize('json', res)
                self.assertEqual(port['port'][psec.PORTSECURITY], True)

                update_port = {'port': {psec.PORTSECURITY: False}}
                req = self.new_update_request('ports', update_port,
                                              port['port']['id'])
                res = req.get_response(self.api)
                self.assertEqual(res.status_int, 409)
                # remove security group on port
                update_port = {'port': {ext_sg.SECURITYGROUPS: None}}
                req = self.new_update_request('ports', update_port,
                                              port['port']['id'])

                self.deserialize('json', req.get_response(self.api))
                self._delete('ports', port['port']['id'])

    def test_update_port_remove_port_security_security_group(self):
        if self._skip_security_group:
            self.skipTest("Plugin does not support security groups")
        with self.network() as net:
            with self.subnet(network=net):
                res = self._create_port('json', net['network']['id'],
                                        arg_list=('port_security_enabled',),
                                        port_security_enabled=True)
                port = self.deserialize('json', res)
                self.assertEqual(port['port'][psec.PORTSECURITY], True)

                # remove security group on port
                update_port = {'port': {ext_sg.SECURITYGROUPS: None,
                                        psec.PORTSECURITY: False}}
                req = self.new_update_request('ports', update_port,
                                              port['port']['id'])

                port = self.deserialize('json', req.get_response(self.api))
                self.assertEqual(port['port'][psec.PORTSECURITY], False)
                self.assertEqual(len(port['port'][ext_sg.SECURITYGROUPS]), 0)
                self._delete('ports', port['port']['id'])

    def test_update_port_remove_port_security_security_group_readd(self):
        if self._skip_security_group:
            self.skipTest("Plugin does not support security groups")
        with self.network() as net:
            with self.subnet(network=net):
                res = self._create_port('json', net['network']['id'],
                                        arg_list=('port_security_enabled',),
                                        port_security_enabled=True)
                port = self.deserialize('json', res)
                self.assertEqual(port['port'][psec.PORTSECURITY], True)

                # remove security group on port
                update_port = {'port': {ext_sg.SECURITYGROUPS: None,
                                        psec.PORTSECURITY: False}}
                req = self.new_update_request('ports', update_port,
                                              port['port']['id'])
                self.deserialize('json', req.get_response(self.api))

                sg_id = port['port'][ext_sg.SECURITYGROUPS]
                update_port = {'port': {ext_sg.SECURITYGROUPS: [sg_id[0]],
                                        psec.PORTSECURITY: True}}

                req = self.new_update_request('ports', update_port,
                                              port['port']['id'])

                port = self.deserialize('json', req.get_response(self.api))
                self.assertEqual(port['port'][psec.PORTSECURITY], True)
                self.assertEqual(len(port['port'][ext_sg.SECURITYGROUPS]), 1)
                self._delete('ports', port['port']['id'])

    def test_create_port_security_off_shared_network(self):
        with self.network(shared=True) as net:
            with self.subnet(network=net):
                res = self._create_port('json', net['network']['id'],
                                        arg_list=('port_security_enabled',),
                                        port_security_enabled=False,
                                        tenant_id='not_network_owner',
                                        set_context=True)
                self.deserialize('json', res)
                self.assertEqual(res.status_int, 403)

    def test_update_port_security_off_shared_network(self):
        with self.network(shared=True, do_delete=False) as net:
            with self.subnet(network=net, do_delete=False):
                res = self._create_port('json', net['network']['id'],
                                        tenant_id='not_network_owner',
                                        set_context=True)
                port = self.deserialize('json', res)
                # remove security group on port
                update_port = {'port': {ext_sg.SECURITYGROUPS: None,
                                        psec.PORTSECURITY: False}}
                req = self.new_update_request('ports', update_port,
                                              port['port']['id'])
                req.environ['quantum.context'] = context.Context(
                    '', 'not_network_owner')
                res = req.get_response(self.api)
                self.assertEqual(res.status_int, 403)
