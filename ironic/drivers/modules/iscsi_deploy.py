# Copyright 2013 Hewlett-Packard Development Company, L.P.
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

import os

from ironic_lib import disk_utils
from ironic_lib import utils as ironic_utils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import fileutils
from six.moves.urllib import parse

from ironic.common import dhcp_factory
from ironic.common import exception
from ironic.common.i18n import _
from ironic.common import keystone
from ironic.common import states
from ironic.common import utils
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.drivers import base
from ironic.drivers.modules import agent_base_vendor
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules import image_cache

LOG = logging.getLogger(__name__)

# NOTE(rameshg87): This file now registers some of opts in pxe group.
# This is acceptable for now as a future refactoring into
# separate boot and deploy interfaces is planned, and moving config
# options twice is not recommended.  Hence we would move the parameters
# to the appropriate place in the final refactoring.
pxe_opts = [
    cfg.StrOpt('pxe_append_params',
               default='nofb nomodeset vga=normal',
               help=_('Additional append parameters for baremetal PXE boot.')),
    cfg.StrOpt('default_ephemeral_format',
               default='ext4',
               help=_('Default file system format for ephemeral partition, '
                      'if one is created.')),
    cfg.StrOpt('images_path',
               default='/var/lib/ironic/images/',
               help=_('On the ironic-conductor node, directory where images '
                      'are stored on disk.')),
    cfg.StrOpt('instance_master_path',
               default='/var/lib/ironic/master_images',
               help=_('On the ironic-conductor node, directory where master '
                      'instance images are stored on disk. '
                      'Setting to <None> disables image caching.')),
    cfg.IntOpt('image_cache_size',
               default=20480,
               help=_('Maximum size (in MiB) of cache for master images, '
                      'including those in use.')),
    # 10080 here is 1 week - 60*24*7. It is entirely arbitrary in the absence
    # of a facility to disable the ttl entirely.
    cfg.IntOpt('image_cache_ttl',
               default=10080,
               help=_('Maximum TTL (in minutes) for old master images in '
                      'cache.')),
    cfg.StrOpt('disk_devices',
               default='cciss/c0d0,sda,hda,vda',
               help=_('The disk devices to scan while doing the deploy.')),
]

iscsi_opts = [
    cfg.PortOpt('portal_port',
                default=3260,
                help=_('The port number on which the iSCSI portal listens '
                       'for incoming connections.')),
]

CONF = cfg.CONF
CONF.register_opts(pxe_opts, group='pxe')
CONF.register_opts(iscsi_opts, group='iscsi')

DISK_LAYOUT_PARAMS = ('root_gb', 'swap_mb', 'ephemeral_gb')


@image_cache.cleanup(priority=50)
class InstanceImageCache(image_cache.ImageCache):

    def __init__(self):
        super(self.__class__, self).__init__(
            CONF.pxe.instance_master_path,
            # MiB -> B
            cache_size=CONF.pxe.image_cache_size * 1024 * 1024,
            # min -> sec
            cache_ttl=CONF.pxe.image_cache_ttl * 60)


def _get_image_dir_path(node_uuid):
    """Generate the dir for an instances disk."""
    return os.path.join(CONF.pxe.images_path, node_uuid)


def _get_image_file_path(node_uuid):
    """Generate the full path for an instances disk."""
    return os.path.join(_get_image_dir_path(node_uuid), 'disk')


def _save_disk_layout(node, i_info):
    """Saves the disk layout.

    The disk layout used for deployment of the node, is saved.

    :param node: the node of interest
    :param i_info: instance information (a dictionary) for the node, containing
                   disk layout information
    """
    driver_internal_info = node.driver_internal_info
    driver_internal_info['instance'] = {}

    for param in DISK_LAYOUT_PARAMS:
        driver_internal_info['instance'][param] = i_info[param]

    node.driver_internal_info = driver_internal_info
    node.save()


def check_image_size(task):
    """Check if the requested image is larger than the root partition size.

    :param task: a TaskManager instance containing the node to act on.
    :raises: InstanceDeployFailure if size of the image is greater than root
        partition.
    """
    i_info = deploy_utils.parse_instance_info(task.node)
    image_path = _get_image_file_path(task.node.uuid)
    image_mb = disk_utils.get_image_mb(image_path)
    root_mb = 1024 * int(i_info['root_gb'])
    if image_mb > root_mb:
        msg = (_('Root partition is too small for requested image. Image '
                 'virtual size: %(image_mb)d MB, Root size: %(root_mb)d MB')
               % {'image_mb': image_mb, 'root_mb': root_mb})
        raise exception.InstanceDeployFailure(msg)


def cache_instance_image(ctx, node):
    """Fetch the instance's image from Glance

    This method pulls the AMI and writes them to the appropriate place
    on local disk.

    :param ctx: context
    :param node: an ironic node object
    :returns: a tuple containing the uuid of the image and the path in
        the filesystem where image is cached.
    """
    i_info = deploy_utils.parse_instance_info(node)
    fileutils.ensure_tree(_get_image_dir_path(node.uuid))
    image_path = _get_image_file_path(node.uuid)
    uuid = i_info['image_source']

    LOG.debug("Fetching image %(ami)s for node %(uuid)s",
              {'ami': uuid, 'uuid': node.uuid})

    deploy_utils.fetch_images(ctx, InstanceImageCache(), [(uuid, image_path)],
                              CONF.force_raw_images)

    return (uuid, image_path)


def destroy_images(node_uuid):
    """Delete instance's image file.

    :param node_uuid: the uuid of the ironic node.
    """
    ironic_utils.unlink_without_raise(_get_image_file_path(node_uuid))
    utils.rmtree_without_raise(_get_image_dir_path(node_uuid))
    InstanceImageCache().clean_up()


def get_deploy_info(node, address, iqn, port=None, lun='1'):
    """Returns the information required for doing iSCSI deploy in a dictionary.

    :param node: ironic node object
    :param address: iSCSI address
    :param iqn: iSCSI iqn for the target disk
    :param port: iSCSI port, defaults to one specified in the configuration
    :param lun: iSCSI lun, defaults to '1'
    :raises: MissingParameterValue, if some required parameters were not
        passed.
    :raises: InvalidParameterValue, if any of the parameters have invalid
        value.
    """
    i_info = deploy_utils.parse_instance_info(node)

    params = {
        'address': address,
        'port': port or CONF.iscsi.portal_port,
        'iqn': iqn,
        'lun': lun,
        'image_path': _get_image_file_path(node.uuid),
        'node_uuid': node.uuid}

    is_whole_disk_image = node.driver_internal_info['is_whole_disk_image']
    if not is_whole_disk_image:
        params.update({'root_mb': 1024 * int(i_info['root_gb']),
                       'swap_mb': int(i_info['swap_mb']),
                       'ephemeral_mb': 1024 * int(i_info['ephemeral_gb']),
                       'preserve_ephemeral': i_info['preserve_ephemeral'],
                       'boot_option': deploy_utils.get_boot_option(node),
                       'boot_mode': _get_boot_mode(node)})

        # Append disk label if specified
        disk_label = deploy_utils.get_disk_label(node)
        if disk_label is not None:
            params['disk_label'] = disk_label

    missing = [key for key in params if params[key] is None]
    if missing:
        raise exception.MissingParameterValue(
            _("Parameters %s were not passed to ironic"
              " for deploy.") % missing)

    if is_whole_disk_image:
        return params

    # configdrive and ephemeral_format are nullable
    params['ephemeral_format'] = i_info.get('ephemeral_format')
    params['configdrive'] = i_info.get('configdrive')

    return params


def continue_deploy(task, **kwargs):
    """Resume a deployment upon getting POST data from deploy ramdisk.

    This method raises no exceptions because it is intended to be
    invoked asynchronously as a callback from the deploy ramdisk.

    :param task: a TaskManager instance containing the node to act on.
    :param kwargs: the kwargs to be passed to deploy.
    :raises: InvalidState if the event is not allowed by the associated
             state machine.
    :returns: a dictionary containing the following keys:
        For partition image:
            'root uuid': UUID of root partition
            'efi system partition uuid': UUID of the uefi system partition
                                         (if boot mode is uefi).
            NOTE: If key exists but value is None, it means partition doesn't
                  exist.
        For whole disk image:
            'disk identifier': ID of the disk to which image was deployed.
    """
    node = task.node

    params = get_deploy_info(node, **kwargs)

    def _fail_deploy(task, msg):
        """Fail the deploy after logging and setting error states."""
        LOG.error(msg)
        deploy_utils.set_failed_state(task, msg)
        destroy_images(task.node.uuid)
        raise exception.InstanceDeployFailure(msg)

    # NOTE(lucasagomes): Let's make sure we don't log the full content
    # of the config drive here because it can be up to 64MB in size,
    # so instead let's log "***" in case config drive is enabled.
    if LOG.isEnabledFor(logging.logging.DEBUG):
        log_params = {
            k: params[k] if k != 'configdrive' else '***'
            for k in params.keys()
        }
        LOG.debug('Continuing deployment for node %(node)s, params %(params)s',
                  {'node': node.uuid, 'params': log_params})

    uuid_dict_returned = {}
    try:
        if node.driver_internal_info['is_whole_disk_image']:
            uuid_dict_returned = deploy_utils.deploy_disk_image(**params)
        else:
            uuid_dict_returned = deploy_utils.deploy_partition_image(**params)
    except Exception as e:
        msg = (_('Deploy failed for instance %(instance)s. '
                 'Error: %(error)s') %
               {'instance': node.instance_uuid, 'error': e})
        _fail_deploy(task, msg)

    root_uuid_or_disk_id = uuid_dict_returned.get(
        'root uuid', uuid_dict_returned.get('disk identifier'))
    if not root_uuid_or_disk_id:
        msg = (_("Couldn't determine the UUID of the root "
                 "partition or the disk identifier after deploying "
                 "node %s") % node.uuid)
        _fail_deploy(task, msg)

    if params.get('preserve_ephemeral', False):
        # Save disk layout information, to check that they are unchanged
        # for any future rebuilds
        _save_disk_layout(node, deploy_utils.parse_instance_info(node))

    destroy_images(node.uuid)
    return uuid_dict_returned


def do_agent_iscsi_deploy(task, agent_client):
    """Method invoked when deployed with the agent ramdisk.

    This method is invoked by drivers for doing iSCSI deploy
    using agent ramdisk.  This method assumes that the agent
    is booted up on the node and is heartbeating.

    :param task: a TaskManager object containing the node.
    :param agent_client: an instance of agent_client.AgentClient
        which will be used during iscsi deploy (for exposing node's
        target disk via iSCSI, for install boot loader, etc).
    :returns: a dictionary containing the following keys:
        For partition image:
            'root uuid': UUID of root partition
            'efi system partition uuid': UUID of the uefi system partition
                                         (if boot mode is uefi).
            NOTE: If key exists but value is None, it means partition doesn't
                  exist.
        For whole disk image:
            'disk identifier': ID of the disk to which image was deployed.
    :raises: InstanceDeployFailure, if it encounters some error
        during the deploy.
    """
    node = task.node
    i_info = deploy_utils.parse_instance_info(node)
    wipe_disk_metadata = not i_info['preserve_ephemeral']

    iqn = 'iqn.2008-10.org.openstack:%s' % node.uuid
    portal_port = CONF.iscsi.portal_port
    result = agent_client.start_iscsi_target(
        node, iqn,
        portal_port,
        wipe_disk_metadata=wipe_disk_metadata)
    if result['command_status'] == 'FAILED':
        msg = (_("Failed to start the iSCSI target to deploy the "
                 "node %(node)s. Error: %(error)s") %
               {'node': node.uuid, 'error': result['command_error']})
        deploy_utils.set_failed_state(task, msg)
        raise exception.InstanceDeployFailure(reason=msg)

    address = parse.urlparse(node.driver_internal_info['agent_url'])
    address = address.hostname

    uuid_dict_returned = continue_deploy(task, iqn=iqn, address=address)
    root_uuid_or_disk_id = uuid_dict_returned.get(
        'root uuid', uuid_dict_returned.get('disk identifier'))

    # TODO(lucasagomes): Move this bit saving the root_uuid to
    # continue_deploy()
    driver_internal_info = node.driver_internal_info
    driver_internal_info['root_uuid_or_disk_id'] = root_uuid_or_disk_id
    node.driver_internal_info = driver_internal_info
    node.save()

    return uuid_dict_returned


def _get_boot_mode(node):
    """Gets the boot mode.

    :param node: A single Node.
    :returns: A string representing the boot mode type. Defaults to 'bios'.
    """
    boot_mode = deploy_utils.get_boot_mode_for_deploy(node)
    if boot_mode:
        return boot_mode
    return "bios"


def validate(task):
    """Validates the pre-requisites for iSCSI deploy.

    Validates whether node in the task provided has some ports enrolled.
    This method validates whether conductor url is available either from CONF
    file or from keystone.

    :param task: a TaskManager instance containing the node to act on.
    :raises: InvalidParameterValue if the URL of the Ironic API service is not
             configured in config file and is not accessible via Keystone
             catalog.
    :raises: MissingParameterValue if no ports are enrolled for the given node.
    """
    try:
        # TODO(lucasagomes): Validate the format of the URL
        CONF.conductor.api_url or keystone.get_service_url()
    except (exception.KeystoneFailure,
            exception.CatalogNotFound,
            exception.KeystoneUnauthorized) as e:
        raise exception.InvalidParameterValue(_(
            "Couldn't get the URL of the Ironic API service from the "
            "configuration file or keystone catalog. Keystone error: %s") % e)

    # Validate the root device hints
    deploy_utils.parse_root_device_hints(task.node)
    deploy_utils.parse_instance_info(task.node)


class ISCSIDeploy(base.DeployInterface):
    """PXE Deploy Interface for deploy-related actions."""

    def get_properties(self):
        return {}

    def validate(self, task):
        """Validate the deployment information for the task's node.

        :param task: a TaskManager instance containing the node to act on.
        :raises: InvalidParameterValue.
        :raises: MissingParameterValue
        """
        task.driver.boot.validate(task)
        node = task.node

        # Check the boot_mode and boot_option capabilities values.
        deploy_utils.validate_capabilities(node)

        # TODO(rameshg87): iscsi_ilo driver uses this method. Remove
        # and copy-paste it's contents here once iscsi_ilo deploy driver
        # broken down into separate boot and deploy implementations.
        validate(task)

    @task_manager.require_exclusive_lock
    def deploy(self, task):
        """Start deployment of the task's node.

        Fetches instance image, updates the DHCP port options for next boot,
        and issues a reboot request to the power driver.
        This causes the node to boot into the deployment ramdisk and triggers
        the next phase of PXE-based deployment via agent heartbeats.

        :param task: a TaskManager instance containing the node to act on.
        :returns: deploy state DEPLOYWAIT.
        """
        node = task.node
        cache_instance_image(task.context, node)
        check_image_size(task)

        manager_utils.node_power_action(task, states.REBOOT)

        return states.DEPLOYWAIT

    @task_manager.require_exclusive_lock
    def tear_down(self, task):
        """Tear down a previous deployment on the task's node.

        Power off the node. All actual clean-up is done in the clean_up()
        method which should be called separately.

        :param task: a TaskManager instance containing the node to act on.
        :returns: deploy state DELETED.
        """
        manager_utils.node_power_action(task, states.POWER_OFF)
        return states.DELETED

    def prepare(self, task):
        """Prepare the deployment environment for this task's node.

        Generates the TFTP configuration for PXE-booting both the deployment
        and user images, fetches the TFTP image from Glance and add it to the
        local cache.

        :param task: a TaskManager instance containing the node to act on.
        """
        node = task.node
        if node.provision_state == states.ACTIVE:
            task.driver.boot.prepare_instance(task)
        else:
            deploy_opts = deploy_utils.build_agent_options(node)
            task.driver.boot.prepare_ramdisk(task, deploy_opts)

    def clean_up(self, task):
        """Clean up the deployment environment for the task's node.

        Unlinks TFTP and instance images and triggers image cache cleanup.
        Removes the TFTP configuration files for this node.

        :param task: a TaskManager instance containing the node to act on.
        """
        destroy_images(task.node.uuid)
        task.driver.boot.clean_up_ramdisk(task)
        task.driver.boot.clean_up_instance(task)
        provider = dhcp_factory.DHCPFactory()
        provider.clean_dhcp(task)

    def take_over(self, task):
        pass

    def get_clean_steps(self, task):
        """Get the list of clean steps from the agent.

        :param task: a TaskManager object containing the node
        :raises NodeCleaningFailure: if the clean steps are not yet
            available (cached), for example, when a node has just been
            enrolled and has not been cleaned yet.
        :returns: A list of clean step dictionaries.
        """
        steps = deploy_utils.agent_get_clean_steps(
            task, interface='deploy',
            override_priorities={
                'erase_devices': CONF.deploy.erase_devices_priority})
        return steps

    def execute_clean_step(self, task, step):
        """Execute a clean step asynchronously on the agent.

        :param task: a TaskManager object containing the node
        :param step: a clean step dictionary to execute
        :raises: NodeCleaningFailure if the agent does not return a command
            status
        :returns: states.CLEANWAIT to signify the step will be completed
            asynchronously.
        """
        return deploy_utils.agent_execute_clean_step(task, step)

    def prepare_cleaning(self, task):
        """Boot into the agent to prepare for cleaning.

        :param task: a TaskManager object containing the node
        :raises NodeCleaningFailure: if the previous cleaning ports cannot
            be removed or if new cleaning ports cannot be created
        :returns: states.CLEANWAIT to signify an asynchronous prepare.
        """
        return deploy_utils.prepare_inband_cleaning(
            task, manage_boot=True)

    def tear_down_cleaning(self, task):
        """Clean up the PXE and DHCP files after cleaning.

        :param task: a TaskManager object containing the node
        :raises NodeCleaningFailure: if the cleaning ports cannot be
            removed
        """
        deploy_utils.tear_down_inband_cleaning(
            task, manage_boot=True)


class VendorPassthru(agent_base_vendor.BaseAgentVendor):
    """Interface to mix IPMI and PXE vendor-specific interfaces."""

    @task_manager.require_exclusive_lock
    def continue_deploy(self, task, **kwargs):
        """Method invoked when deployed using iSCSI.

        This method is invoked during a heartbeat from an agent when
        the node is in wait-call-back state. This deploys the image on
        the node and then configures the node to boot according to the
        desired boot option (netboot or localboot).

        :param task: a TaskManager object containing the node.
        :param kwargs: the kwargs passed from the heartbeat method.
        :raises: InstanceDeployFailure, if it encounters some error during
            the deploy.
        """
        task.process_event('resume')
        node = task.node
        LOG.debug('Continuing the deployment on node %s', node.uuid)

        uuid_dict_returned = do_agent_iscsi_deploy(task, self._client)
        root_uuid = uuid_dict_returned.get('root uuid')
        efi_sys_uuid = uuid_dict_returned.get('efi system partition uuid')
        self.prepare_instance_to_boot(task, root_uuid, efi_sys_uuid)
        self.reboot_and_finish_deploy(task)
