# vim: tabstop=4 shiftwidth=4 softtabstop=4

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

import collections
import copy
import datetime
import logging
import random

import django.conf
import django.db.models
from django.utils.translation import ugettext_lazy as _  # noqa
from horizon import exceptions
import requests

from novaclient.v1_1.contrib import baremetal
from tuskarclient.v1 import client as tuskar_client

from openstack_dashboard.api import base
from openstack_dashboard.api import nova


LOG = logging.getLogger(__name__)
TUSKAR_ENDPOINT_URL = getattr(django.conf.settings, 'TUSKAR_ENDPOINT_URL')
REMOTE_NOVA_BAREMETAL_CREDS = getattr(django.conf.settings,
                                      'REMOTE_NOVA_BAREMETAL_CREDS',
                                      False)
OVERCLOUD_CREDS = getattr(django.conf.settings, 'OVERCLOUD_CREDS', False)


# FIXME: request isn't used right in the tuskar client right now, but looking
# at other clients, it seems like it will be in the future
def tuskarclient(request):
    c = tuskar_client.Client(TUSKAR_ENDPOINT_URL)
    return c


def baremetalclient(request):
    def create_remote_nova_client_baremetal():
        nc = nova.nova_client.Client(REMOTE_NOVA_BAREMETAL_CREDS['user'],
                        REMOTE_NOVA_BAREMETAL_CREDS['password'],
                        REMOTE_NOVA_BAREMETAL_CREDS['tenant'],
                        auth_url=REMOTE_NOVA_BAREMETAL_CREDS['auth_url'],
                        bypass_url=REMOTE_NOVA_BAREMETAL_CREDS['bypass_url'])
        return nc

    def create_nova_client_baremetal():
        insecure = getattr(django.conf.settings, 'OPENSTACK_SSL_NO_VERIFY',
                           False)
        nc = nova.nova_client.Client(
                request.user.username,
                request.user.token.id,
                project_id=request.user.tenant_id,
                auth_url=base.url_for(request, 'compute'),
                insecure=insecure,
                http_log_debug=django.conf.settings.DEBUG)
        nc.client.auth_token = request.user.token.id
        nc.client.management_url = base.url_for(request, 'compute')

        return nc

    if REMOTE_NOVA_BAREMETAL_CREDS:
        LOG.debug('remote nova baremetal client connection created')
        nc = create_remote_nova_client_baremetal()
    else:
        LOG.debug('nova baremetal client connection created using token "%s" '
                  'and url "%s"' %
                  (request.user.token.id, base.url_for(request, 'compute')))
        nc = create_nova_client_baremetal()

    return baremetal.BareMetalNodeManager(nc)


def overcloudclient(request):
    c = nova.nova_client.Client(OVERCLOUD_CREDS['user'],
                                OVERCLOUD_CREDS['password'],
                                OVERCLOUD_CREDS['tenant'],
                                auth_url=OVERCLOUD_CREDS['auth_url'])
    return c


class StringIdAPIResourceWrapper(base.APIResourceWrapper):
    # horizon DataTable class expects ids to be string,
    # if it's not string, then comparison in
    # horizon/tables/base.py:get_object_by_id fails.
    # Because of this, ids returned from dummy api are converted to string
    # (luckily django autoconverts strings to integers when passing string to
    # django model id)

    def __init__(self, apiresource, request=None):
        self.request = request
        self._apiresource = apiresource

    # FIXME
    # this is redefined from base.APIResourceWrapper,
    # remove this when tuskarclient returns object instead of dict
    def __getattr__(self, attr):
        if attr in self._attrs:
            if issubclass(self._apiresource.__class__, dict):
                return self._apiresource.get(attr)
            else:
                return self._apiresource.__getattribute__(attr)
        else:
            raise AttributeError(attr)

    @property
    def id(self):
        return str(self._apiresource.id)

    # FIXME: self.request is required when calling some instance
    # methods (e.g. list_flavors), once we really start using this request
    # param (if ever), a proper request value should be set
    @property
    def request(self):
        return getattr(self, '_request', None)

    @request.setter
    def request(self, value):
        setattr(self, '_request', value)


class Alert(StringIdAPIResourceWrapper):
    """Wrapper for the Alert object returned by the
    dummy model.
    """
    _attrs = ['message', 'time']


class Capacity(StringIdAPIResourceWrapper):
    """Wrapper for the Capacity object returned by the
    dummy model.
    """
    _attrs = ['name', 'value', 'unit']

    # defines a random usage of capacity - API should probably be able to
    # determine usage of capacity based on capacity value and obejct_id
    @property
    def usage(self):
        if not hasattr(self, '_usage'):
            self._usage = random.randint(0, int(self.value))
        return self._usage

    # defines a random average of capacity - API should probably be able to
    # determine average of capacity based on capacity value and obejct_id
    @property
    def average(self):
        if not hasattr(self, '_average'):
            self._average = random.randint(0, int(self.value))
        return self._average


class Node(StringIdAPIResourceWrapper):
    """Wrapper for the Node object  returned by the
    dummy model.
    """
    _attrs = ['id', 'pm_address', 'cpus', 'memory_mb', 'service_host',
              'local_gb', 'pm_user']

    @classmethod
    def get(cls, request, node_id):
        node = cls(baremetalclient(request).get(node_id))
        node.request = request

        # FIXME ugly, fix after demo, make abstraction of instance details
        # this is realy not optimal, but i dont hve time do fix it now
        instances, more = nova.server_list(
            request,
            search_opts={'paginate': True},
            all_tenants=True)

        instance_details = {}
        for instance in instances:
            id = (instance.
                  _apiresource._info['OS-EXT-SRV-ATTR:hypervisor_hostname'])
            instance_details[id] = instance

        detail = instance_details.get(node_id)
        if detail:
            addresses = detail._apiresource.addresses.get('ctlplane')
            if addresses:
                node.ip_address_other = (", "
                    .join([addr['addr'] for addr in addresses]))

            node.status = detail._apiresource._info['OS-EXT-STS:vm_state']
            node.power_management = ""
            if node.pm_user:
                node.power_management = node.pm_user + "/********"
        else:
            node.status = 'unprovisioned'

        return node

    @classmethod
    def list(cls, request):
        return [Node(n, request) for n in
                baremetalclient(request).list()]

    @classmethod
    def list_unracked(cls, request):
        try:
            return [n for n in Node.list(request) if (n.rack is None)]
        except requests.ConnectionError:
            return []

    @classmethod
    def create(cls, request, **kwargs):
        node = baremetalclient(request).create(kwargs['name'],
                                               kwargs['cpus'],
                                               kwargs['memory_mb'],
                                               kwargs['local_gb'],
                                               kwargs['prov_mac_address'],
                                               kwargs['pm_address'],
                                               kwargs['pm_user'],
                                               kwargs['pm_password'],
                                               kwargs['terminal_port'])
        return cls(node)

    @property
    def list_flavors(self):
        if not hasattr(self, '_flavors'):
            # FIXME: just a mock of used instances, add real values
            used_instances = 0

            if not self.rack or not self.rack.get_resource_class:
                return []
            resource_class = self.rack.get_resource_class

            added_flavors = tuskarclient(self.request).flavors\
                                                      .list(resource_class.id)
            self._flavors = []
            if added_flavors:
                for f in added_flavors:
                    flavor_obj = Flavor(f)
                    #flavor_obj.max_vms = f.max_vms

                    # FIXME just a mock of used instances, add real values
                    used_instances += 5
                    flavor_obj.used_instances = used_instances
                    self._flavors.append(flavor_obj)

        return self._flavors

    @property
    def rack(self):
        try:
            if not hasattr(self, '_rack'):
                # FIXME the node.rack association should be stored somewhere
                self._rack = None
                for rack in Rack.list(self.request):
                    for node_obj in rack.list_nodes:
                        if node_obj.id == self.id:
                            self._rack = rack

            return self._rack
        except Exception:
            msg = "Could not obtain Nodes's rack"
            LOG.debug(exceptions.error_color(msg))
            return None

    @property
    # FIXME: just mock implementation, add proper one
    def running_instances(self):
        return 4

    @property
    # FIXME: just mock implementation, add proper one
    def remaining_capacity(self):
        return 100 - self.running_instances

    @property
    # FIXME: just mock implementation, add proper one
    def is_provisioned(self):
        return self.status != "unprovisioned" and self.rack

    @property
    def alerts(self):
        if not hasattr(self, '_alerts'):
            self._alerts = []
        return self._alerts

    @property
    def mac_address(self):
        try:
            return self._apiresource.interfaces[0]['address']
        except Exception:
            return None

    @property
    def running_virtual_machines(self):
        if not hasattr(self, '_running_virtual_machines'):
            if OVERCLOUD_CREDS:
                search_opts = {}
                search_opts['all_tenants'] = True
                self._running_virtual_machines = [s for s in
                    overcloudclient(self.request).servers
                        .list(True, search_opts)
                    if s.hostId == self.id]
            else:
                LOG.debug('OVERCLOUD_CREDS is not set. '
                          'Can\'t connect to Overcloud')
                self._running_virtual_machines = []
        return self._running_virtual_machines


class Rack(StringIdAPIResourceWrapper):
    """Wrapper for the Rack object  returned by the
    dummy model.
    """
    _attrs = ['id', 'name', 'location', 'subnet', 'nodes', 'state',
              'capacities', 'resource_class']

    @classmethod
    def create(cls, request, **kwargs):
        nodes = kwargs.get('nodes', [])
        ## FIXME: set nodes here
        rack = tuskarclient(request).racks.create(
                name=kwargs['name'],
                location=kwargs['location'],
                subnet=kwargs['subnet'],
                nodes=nodes,
                resource_class={'id': kwargs['resource_class_id']},
                slots=0)
        return cls(rack)

    @classmethod
    def update(cls, request, rack_id, rack_kwargs):
        ## FIXME: set nodes here
        rack_args = copy.copy(rack_kwargs)
        # remove rack_id from kwargs (othervise it is duplicated)
        rack_args.pop('rack_id', None)
        # correct data mapping for resource_class
        if 'resource_class_id' in rack_args:
            rack_args['resource_class'] = {
                'id': rack_args.pop('resource_class_id', None)}

        rack = tuskarclient(request).racks.update(rack_id, **rack_args)
        return cls(rack)

    @classmethod
    def list(cls, request, only_free_racks=False):
        if only_free_racks:
            return [Rack(r, request) for r in
                    tuskarclient(request).racks.list() if (
                        r.resource_class is None)]
        else:
            return [Rack(r, request) for r in
                    tuskarclient(request).racks.list()]

    @classmethod
    def get(cls, request, rack_id):
        rack = cls(tuskarclient(request).racks.get(rack_id))
        rack.request = request
        return rack

    @classmethod
    def delete(cls, request, rack_id):
        tuskarclient(request).racks.delete(rack_id)

    @property
    def node_ids(self):
        """ List of unicode ids of nodes added to rack"""
        return [
            unicode(node['id']) for node in (
                self.nodes)]

    @property
    def list_nodes(self):
        if not hasattr(self, '_nodes'):
            self._nodes = [Node.get(self.request, node['id']) for node in
                           self.nodes]
        return self._nodes

    @property
    def nodes_count(self):
        return len(self.nodes)

    @property
    def resource_class_id(self):
        rclass = self.resource_class
        return rclass['id'] if rclass else None

    @property
    def get_resource_class(self):
        if not hasattr(self, '_resource_class'):
            rclass = self.resource_class
            if rclass:
                self._resource_class = ResourceClass.get(self.request,
                                                         rclass['id'])
            else:
                self._resource_class = None
        return self._resource_class

    @property
    def list_capacities(self):
        if not hasattr(self, '_capacities'):
            self._capacities = [Capacity(c) for c in self.capacities]
        return self._capacities

    @property
    def vm_capacity(self):
        """ Rack VM Capacity is maximum value from its Resource Class's
            Flavors max_vms (considering flavor sizes are multiples).
        """
        if not hasattr(self, '_vm_capacity'):
            try:
                value = max([flavor.max_vms for flavor in
                             self.get_resource_class.list_flavors])
            except Exception:
                value = None
            self._vm_capacity = Capacity({'name': "VM Capacity",
                                          'value': value,
                                          'unit': 'VMs'})
        return self._vm_capacity

    @property
    def alerts(self):
        if not hasattr(self, '_alerts'):
            self._alerts = []
        return self._alerts

    @property
    def aggregated_alerts(self):
        # FIXME: for now return only list of nodes (particular alerts are not
        # used)
        return [node for node in self.list_nodes if node.alerts]

    @property
    def list_flavors(self):
        if not hasattr(self, '_flavors'):
            # FIXME just a mock of used instances, add real values
            used_instances = 0

            if not self.get_resource_class:
                return []
            added_flavors = tuskarclient(self.request).flavors\
                                .list(self.get_resource_class.id)
            self._flavors = []
            if added_flavors:
                for f in added_flavors:
                    flavor_obj = Flavor(f)
                    #flavor_obj.max_vms = f.max_vms

                    # FIXME just a mock of used instances, add real values
                    used_instances += 2
                    flavor_obj.used_instances = used_instances
                    self._flavors.append(flavor_obj)

        return self._flavors

    @property
    def all_used_instances(self):
        return [flavor.used_instances for flavor in self.list_flavors]

    @property
    def total_instances(self):
        # FIXME just mock implementation, add proper one
        return sum(self.all_used_instances)

    @property
    def remaining_capacity(self):
        # FIXME just mock implementation, add proper one
        return 100 - self.total_instances

    @property
    def is_provisioned(self):
        return (self.state == 'active') or (self.state == 'error')

    @property
    def is_provisioning(self):
        return (self.state == 'provisioning')

    @classmethod
    def provision(cls, request, rack_id):
        tuskarclient(request).data_centers.provision_all()


class ResourceClass(StringIdAPIResourceWrapper):
    """Wrapper for the ResourceClass object  returned by the
    dummy model.
    """
    _attrs = ['id', 'name', 'service_type', 'racks']

    @classmethod
    def get(cls, request, resource_class_id):
        rc = cls(tuskarclient(request).resource_classes.get(resource_class_id))
        rc.request = request
        return rc

    @classmethod
    def create(self, request, **kwargs):
        return ResourceClass(
            tuskarclient(request).resource_classes.create(
                name=kwargs['name'],
                service_type=kwargs['service_type'],
                flavors=kwargs['flavors']))

    @classmethod
    def list(cls, request):
        return [cls(rc, request) for rc in (
            tuskarclient(request).resource_classes.list())]

    @classmethod
    ## FIXME : kwargs here is a little dicey
    def update(cls, request, resource_class_id, **kwargs):
        resource_class = cls(tuskarclient(request).resource_classes.update(
                resource_class_id, **kwargs))

        ## FIXME: flavors have to be updated separately, seems less than ideal
        for flavor_id in resource_class.flavors_ids:
            Flavor.delete(request, resource_class_id=resource_class.id,
                                   flavor_id=flavor_id)
        for flavor in kwargs['flavors']:
            Flavor.create(request,
                          resource_class_id=resource_class.id,
                          **flavor)

        return resource_class

    @property
    def deletable(self):
        return (len(self.racks) <= 0)

    @classmethod
    def delete(cls, request, resource_class_id):
        tuskarclient(request).resource_classes.delete(resource_class_id)

    @property
    def racks_ids(self):
        """ List of unicode ids of racks added to resource class """
        return [
            unicode(rack['id']) for rack in self.racks]

    @property
    def list_racks(self):
        """ List of racks added to ResourceClass """
        if not hasattr(self, '_racks'):
            self._racks = [Rack.get(self.request, rid) for rid in (
                self.racks_ids)]
        return self._racks

    def set_racks(self, request, racks_ids):
        # FIXME: there is a bug now in tuskar, we have to remove all racks at
        # first and then add new ones:
        # https://github.com/tuskar/tuskar/issues/37
        tuskarclient(request).resource_classes.update(self.id, racks=[])
        racks = [{'id': rid} for rid in racks_ids]
        tuskarclient(request).resource_classes.update(self.id, racks=racks)

    @property
    def racks_count(self):
        return len(self.racks)

    @property
    def all_racks(self):
        """ List of racks added to ResourceClass + list of free racks,
        meaning racks that don't belong to any ResourceClass"""
        if not hasattr(self, '_all_racks'):
            self._all_racks =\
                [r for r in (
                    Rack.list(self.request)) if (
                        r.resource_class_id is None or
                        str(r.resource_class_id) == self.id)]
        return self._all_racks

    @property
    def nodes(self):
        if not hasattr(self, '_nodes'):
            nodes_lists = [rack.list_nodes for rack in self.list_racks]
            self._nodes = [node for nodes in nodes_lists for node in nodes]
        return self._nodes

    @property
    def nodes_count(self):
        return len(self.nodes)

    @property
    def flavors_ids(self):
        """ List of unicode ids of flavors added to resource class """
        return [unicode(flavor.id) for flavor in self.list_flavors]

    @property
    def list_flavors(self):
        if not hasattr(self, '_flavors'):
            # FIXME just a mock of used instances, add real values
            used_instances = 0

            added_flavors = tuskarclient(self.request).flavors.list(self.id)
            self._flavors = []
            for f in added_flavors:
                flavor_obj = Flavor(f, self.request)
                #flavor_obj.max_vms = f.max_vms

                # FIXME just a mock of used instances, add real values
                used_instances += 5
                flavor_obj.used_instances = used_instances
                self._flavors.append(flavor_obj)
        return self._flavors

    @property
    def all_used_instances(self):
        return [flavor.used_instances for flavor in self.list_flavors]

    @property
    def total_instances(self):
        # FIXME just mock implementation, add proper one
        return sum(self.all_used_instances)

    @property
    def remaining_capacity(self):
        # FIXME just mock implementation, add proper one
        return 100 - self.total_instances

    @property
    def capacities(self):
        """Aggregates Rack capacities values
        """
        if not hasattr(self, '_capacities'):
            capacities = [rack.list_capacities for rack in self.list_racks]

            def add_capacities(c1, c2):
                return [Capacity({'name': a.name,
                                 'value': int(a.value) + int(b.value),
                                 'unit': a.unit}) for a, b in zip(c1, c2)]

            self._capacities = reduce(add_capacities, capacities)
        return self._capacities

    @property
    def vm_capacity(self):
        """ Resource Class VM Capacity is maximum value from It's Flavors
            max_vms (considering flavor sizes are multiples), multipled by
            number of Racks in Resource Class.
        """
        if not hasattr(self, '_vm_capacity'):
            try:
                value = self.racks_count * max([flavor.max_vms for flavor in
                                                self.list_flavors])
            except Exception:
                value = _("Unable to retrieve vm capacity")
            self._vm_capacity = Capacity({'name': _("VM Capacity"),
                                          'value': value,
                                          'unit': _('VMs')})
        return self._vm_capacity

    @property
    def aggregated_alerts(self):
        # FIXME: for now return only list of racks (particular alerts are not
        # used)
        return [rack for rack in self.list_racks if (rack.alerts +
            rack.aggregated_alerts)]

    @property
    def has_provisioned_rack(self):
        return any([rack.is_provisioned for rack in self.list_racks])


class Flavor(StringIdAPIResourceWrapper):
    """Wrapper for the Flavor object returned by Tuskar.
    """
    _attrs = ['id', 'name', 'max_vms']

    @classmethod
    def get(cls, request, resource_class_id, flavor_id):
        flavor = cls(tuskarclient(request).flavors.get(resource_class_id,
                                                       flavor_id))
        flavor.request = request
        return flavor

    @classmethod
    def create(cls, request, **kwargs):
        return cls(tuskarclient(request).flavors.create(
                kwargs['resource_class_id'],
                name=kwargs['name'],
                max_vms=kwargs['max_vms'],
                capacities=kwargs['capacities']))

    @classmethod
    def delete(cls, request, **kwargs):
        tuskarclient(request).flavors.delete(
                                kwargs['resource_class_id'],
                                kwargs['flavor_id'])

    @property
    def capacities(self):
        if not hasattr(self, '_capacities'):
            ## FIXME: should we distinguish between tuskar
            ## capacities and our internal capacities?
            CapacityStruct = collections.namedtuple('CapacityStruct',
                                                    'name value unit')
            self._capacities = [Capacity(CapacityStruct(
                        name=c['name'],
                        value=c['value'],
                        unit=c['unit'])) for c in self._apiresource.capacities]
        return self._capacities

    def capacity(self, capacity_name):
        key = "_%s" % capacity_name
        if not hasattr(self, key):
            try:
                capacity = [c for c in self.capacities if (
                    c.name == capacity_name)][0]
            except Exception:
                # FIXME: test this
                capacity = Capacity(
                    name=capacity_name,
                    value=_('Unable to retrieve '
                            '(Is the flavor configured properly?)'),
                    unit='')
            setattr(self, key, capacity)
        return getattr(self, key)

    @property
    def cpu(self):
        return self.capacity('cpu')

    @property
    def memory(self):
        return self.capacity('memory')

    @property
    def storage(self):
        return self.capacity('storage')

    @property
    def ephemeral_disk(self):
        return self.capacity('ephemeral_disk')

    @property
    def swap_disk(self):
        return self.capacity('swap_disk')

    @property
    def running_virtual_machines(self):
        # FIXME: arbitrary number
        return random.randint(0, int(self.cpu.value))

    # defines a random average of capacity - API should probably be able to
    # determine average of capacity based on capacity value and obejct_id
    def vms_over_time(self, start_time, end_time):
        values = []
        current_time = start_time
        while current_time <= end_time:
            values.append(
                {'date': current_time,
                 'value': random.randint(0, self.running_virtual_machines)})
            current_time += datetime.timedelta(hours=1)

        return values
