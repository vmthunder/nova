#   Copyright 2011 OpenStack Foundation
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.

"""Connect your vlan to the world."""

from oslo.config import cfg
from webob import exc

from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.cloudpipe import pipelib
from nova import compute
from nova.compute import utils as compute_utils
from nova.compute import vm_states
from nova import exception
from nova.i18n import _
from nova import network
from nova.openstack.common import fileutils
from nova.openstack.common import timeutils
from nova import utils

CONF = cfg.CONF
CONF.import_opt('keys_path', 'nova.crypto')

ALIAS = 'os-cloudpipe'
authorize = extensions.extension_authorizer('compute', 'v3:' + ALIAS)


class CloudpipeController(wsgi.Controller):
    """Handle creating and listing cloudpipe instances."""

    def __init__(self):
        self.compute_api = compute.API()
        self.network_api = network.API()
        self.cloudpipe = pipelib.CloudPipe()
        self.setup()

    def setup(self):
        """Ensure the keychains and folders exist."""
        # NOTE(vish): One of the drawbacks of doing this in the api is
        #             the keys will only be on the api node that launched
        #             the cloudpipe.
        fileutils.ensure_tree(CONF.keys_path)

    def _get_all_cloudpipes(self, context):
        """Get all cloudpipes."""
        instances = self.compute_api.get_all(context,
                                             search_opts={'deleted': False})
        return [instance for instance in instances
                if pipelib.is_vpn_image(instance['image_ref'])
                and instance['vm_state'] != vm_states.DELETED]

    def _get_cloudpipe_for_project(self, context, project_id):
        """Get the cloudpipe instance for a project ID."""
        cloudpipes = self._get_all_cloudpipes(context) or [None]
        return cloudpipes[0]

    def _get_ip_and_port(self, instance):
        pass

    def _vpn_dict(self, context, project_id, instance):
        elevated = context.elevated()
        rv = {'project_id': project_id}
        if not instance:
            rv['state'] = 'pending'
            return rv
        rv['instance_id'] = instance['uuid']
        rv['created_at'] = timeutils.isotime(instance['created_at'])
        nw_info = compute_utils.get_nw_info_for_instance(instance)
        if not nw_info:
            return rv
        vif = nw_info[0]
        ips = [ip for ip in vif.fixed_ips() if ip['version'] == 4]
        if ips:
            rv['internal_ip'] = ips[0]['address']
        # NOTE(vish): Currently network_api.get does an owner check on
        #             project_id. This is probably no longer necessary
        #             but rather than risk changes in the db layer,
        #             we are working around it here by changing the
        #             project_id in the context. This can be removed
        #             if we remove the project_id check in the db.
        elevated.project_id = project_id
        network = self.network_api.get(elevated, vif['network']['id'])
        if network:
            vpn_ip = network['vpn_public_address']
            vpn_port = network['vpn_public_port']
            rv['public_ip'] = vpn_ip
            rv['public_port'] = vpn_port
            if vpn_ip and vpn_port:
                if utils.vpn_ping(vpn_ip, vpn_port):
                    rv['state'] = 'running'
                else:
                    rv['state'] = 'down'
            else:
                rv['state'] = 'invalid'
        return rv

    @extensions.expected_errors((400, 403))
    def create(self, req, body):
        """Create a new cloudpipe instance, if none exists.

        Parameters: {cloudpipe: {'project_id': ''}}
        """

        context = req.environ['nova.context']
        authorize(context)
        params = body.get('cloudpipe', {})
        project_id = params.get('project_id', context.project_id)
        # NOTE(vish): downgrade to project context. Note that we keep
        #             the same token so we can still talk to glance
        context.project_id = project_id
        context.user_id = 'project-vpn'
        context.is_admin = False
        context.roles = []
        instance = self._get_cloudpipe_for_project(context, project_id)
        if not instance:
            try:
                result = self.cloudpipe.launch_vpn_instance(context)
                instance = result[0][0]
            except exception.NoMoreNetworks:
                msg = _("Unable to claim IP for VPN instances, ensure it "
                        "isn't running, and try again in a few minutes")
                raise exc.HTTPBadRequest(explanation=msg)
        return {'instance_id': instance['uuid']}

    @extensions.expected_errors((400, 403, 404))
    def index(self, req):
        """List running cloudpipe instances."""
        context = req.environ['nova.context']
        authorize(context)
        vpns = [self._vpn_dict(context, x['project_id'], x)
                for x in self._get_all_cloudpipes(context)]
        return {'cloudpipes': vpns}


class Cloudpipe(extensions.V3APIExtensionBase):
    """Adds actions to create cloudpipe instances.

    When running with the Vlan network mode, you need a mechanism to route
    from the public Internet to your vlans.  This mechanism is known as a
    cloudpipe.

    At the time of creating this class, only OpenVPN is supported.  Support for
    a SSH Bastion host is forthcoming.
    """

    name = "Cloudpipe"
    alias = ALIAS
    version = 1

    def get_resources(self):
        resource = [extensions.ResourceExtension(ALIAS,
                                           CloudpipeController())]
        return resource

    def get_controller_extensions(self):
        """It's an abstract function V3APIExtensionBase and the extension
        will not be loaded without it.
        """
        return []
