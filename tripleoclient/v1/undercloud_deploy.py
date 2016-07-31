#   Copyright 2015 Red Hat, Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.
#
from __future__ import print_function

import eventlet
eventlet.monkey_patch(os=False)

import argparse
import glob
import logging
import os
import platform
import signal
import subprocess
import tempfile
import time
import urllib2

from cliff import command
from heatclient.common import template_utils
from openstackclient.i18n import _

from tripleoclient import constants
from tripleoclient import exceptions
from tripleoclient import fake_keystone


class DeployUndercloud(command.Command):
    """Deploy Undercloud"""

    log = logging.getLogger(__name__ + ".DeployUndercloud")
    auth_required = False

    def _launch_local_heat(self, heat_conf):
        os.execvp('heat-all', ['heat-all', '--config-file', heat_conf])

    def _heat_db_sync(self, heat_conf):
        subprocess.check_call(['heat-manage', '--config-file', heat_conf,
                              'db_sync'])

    def _lookup_deployed_server_stack_id(self, client, stack_id):
        server_stack_id = None
        for X in client.resources.list(stack_id, nested_depth=5):
            if X.resource_type == 'OS::TripleO::Server':
                server_stack_id = X.physical_resource_id

        deployed_server_stack = None
        if server_stack_id:
            for X in client.resources.list(server_stack_id, nested_depth=5):
                if X.resource_name == 'deployed-server':
                    deployed_server_stack = X.physical_resource_id

        return deployed_server_stack

    def _launch_os_collect_config(self, keystone_port, stack_id):
        os.execvp('os-collect-config',
                  ['os-collect-config',
                   '--polling-interval', '3',
                   '--heat-auth-url', 'http://127.0.0.1:%s/v3' % keystone_port,
                   '--heat-password', 'fake',
                   '--heat-user-id', 'admin',
                   '--heat-project-id', 'admin',
                   '--heat-stack-id', stack_id,
                   '--heat-resource-name',
                   'deployed-server-config', 'heat'])

    def _default_api_paste_ini(self):
        ini_files = ['/usr/share/heat/api-paste-dist.ini',
                     '/etc/heat/api-paste.ini']
        for f in ini_files:
            if os.path.exists(f):
                return f
        return None

    def _wait_local_port_ready(self, api_port):
        count = 0
        while count < 30:
            time.sleep(1)
            count += 1
            try:
                urllib2.urlopen("http://127.0.0.1:%s/" % api_port, timeout=1)
            except urllib2.HTTPError as he:
                if he.code == 300:
                    return True
                pass
            except urllib2.URLError:
                pass
        return False

    def _create_heat_config(self, sqlite_db, api_paste_ini, api_port, ks_port):
        policy_file = os.path.join(os.path.dirname(__file__),
                                   'noauth_policy.json')
        heat_config = '''
[DEFAULT]
rpc_backend = fake
deferred_auth_method = password
num_engine_workers=1

default_deployment_signal_transport = HEAT_SIGNAL

[heat_all]
enabled_services = api,engine

[heat_api]
workers = 1
bind_host = 127.0.0.1
bind_port = %(api_port)s

[database]
connection = sqlite:///%(sqlite_db)s.db

[paste_deploy]
flavor = noauth
api_paste_config = %(api_paste_ini)s

[oslo_policy]
policy_file = %(policy_file)s

[clients_keystone]
auth_uri=http://127.0.0.1:%(ks_port)s

[clients_keystone]
auth_uri=http://127.0.0.1:35358

[keystone_authtoken]
auth_type = password
auth_url=http://127.0.0.1:%(ks_port)s
        ''' % {'sqlite_db': sqlite_db, 'policy_file': policy_file,
               'api_paste_ini': api_paste_ini,
               'api_port': api_port,
               'ks_port': ks_port,
               'policy_file': policy_file}
        handle, heat_config_file = tempfile.mkstemp()
        with open(heat_config_file, 'w') as temp_file:
            temp_file.write(heat_config)
        os.close(handle)
        return heat_config_file

    def _install_base(self):
        """Install base dependencies for os-collect-config hooks.

        We use instack to install these things locally because we still
        rely on most of these elements to install these things in the
        overcloud as well.
        """

        env = {}
        distro = platform.linux_distribution()[0]
        if os.environ.get('NODE_DIST'):
            node_dist = os.environ.get('NODE_DIST')
        else:
            if distro.startswith('Red Hat Enterprise Linux'):
                node_dist = 'rhel7'
            elif distro.startswith('CentOS'):
                node_dist = 'centos7'
            elif distro.startswith('Fedora'):
                node_dist = 'fedora'
            else:
                raise RuntimeError('%s is not supported' % distro)

        if 'ELEMENTS_PATH' in os.environ:
            env['ELEMENTS_PATH'] = os.environ['ELEMENTS_PATH']
        else:
            env['ELEMENTS_PATH'] = ('/usr/share/openstack-heat-templates/'
                                    'software-config/elements/:/usr/share/'
                                    'tripleo-image-elements:/usr/share/'
                                    'diskimage-builder/elements:'
                                    '/usr/share/tripleo-puppet-elements/')

        args = ['sudo', '-E', 'instack',
                '-e', node_dist, 'enable-packages-install',
                'os-collect-config', 'heat-config', 'puppet-modules',
                'heat-config-os-apply-config', 'hiera',
                'heat-config-puppet', 'heat-config-script',
                '-k', 'environment', 'extra-data', 'pre-install', 'install',
                'post-install',
                '-b', '15-remove-grub', '10-cloud-init',
                '05-fstab-rootfs-label']

        subprocess.check_call(args, env=env)

    def _heat_deploy(self, stack_name, template_path, parameters,
                     environments, timeout, api_port, ks_port):
        self.log.debug("Processing environment files")
        env_files, env = (
            template_utils.process_multiple_environments_and_files(
                environments))

        self.log.debug("Getting template contents")
        template_files, template = template_utils.get_template_contents(
            template_path)

        files = dict(list(template_files.items()) + list(env_files.items()))

        # NOTE(dprince): we use our own client here because we set
        # auth_required=False above because keystone isn't running when this
        # command starts
        tripleoclients = self.app.client_manager.tripleoclient
        orchestration_client = tripleoclients.local_orchestration(api_port,
                                                                  ks_port)

        self.log.debug("Deploying stack: %s", stack_name)
        self.log.debug("Deploying template: %s", template)
        self.log.debug("Deploying parameters: %s", parameters)
        self.log.debug("Deploying environment: %s", env)
        self.log.debug("Deploying files: %s", files)

        stack_args = {
            'stack_name': stack_name,
            'template': template,
            'environment': env,
            'files': files,
        }

        if timeout:
            stack_args['timeout_mins'] = timeout

        self.log.info("Performing Heat stack create")
        stack = orchestration_client.stacks.create(**stack_args)
        stack_id = stack['stack']['id']

        server_stack_id = None
        # NOTE(dprince) wait a bit to create the server_stack_id resource
        for c in range(10):
            time.sleep(1)
            server_stack_id = self._lookup_deployed_server_stack_id(
                orchestration_client, stack_id)
            if server_stack_id:
                break
        if not server_stack_id:
            msg = ('Unable to find deployed server stack id. '
                   'See tripleo-heat-templates to ensure proper '
                   '"deployed-server" usage.')
            raise Exception(msg)

        pid = None
        status = 'FAILED'
        try:
            pid = os.fork()
            if pid == 0:
                self._launch_os_collect_config(ks_port, server_stack_id)
            else:
                while True:
                    status = orchestration_client.stacks.get(stack_id).status
                    self.log.info(status)
                    if status in ['COMPLETE', 'FAILED']:
                        break
                    time.sleep(5)
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)

        if status == 'FAILED':
            return False
        else:
            return True

    def _load_environment_directories(self, directories):
        if os.environ.get('TRIPLEO_ENVIRONMENT_DIRECTORY'):
            directories.append(os.environ.get('TRIPLEO_ENVIRONMENT_DIRECTORY'))

        environments = []
        for d in directories:
            if os.path.exists(d) and d != '.':
                self.log.debug("Environment directory: %s" % d)
                for f in sorted(glob.glob(os.path.join(d, '*.yaml'))):
                    self.log.debug("Environment directory file: %s" % f)
                    if os.path.isfile(f):
                        environments.append(f)
        return environments

    def _deploy_tripleo_heat_templates(self, parsed_args):
        """Deploy the fixed templates in TripleO Heat Templates"""
        parameters = {}
        tht_root = parsed_args.templates

        print("Deploying templates in the directory {0}".format(
            os.path.abspath(tht_root)))

        self.log.debug("Creating Environment file")
        environments = []

        if parsed_args.environment_directories:
            environments.extend(self._load_environment_directories(
                parsed_args.environment_directories))

        if parsed_args.environment_files:
            environments.extend(parsed_args.environment_files)

        resource_registry_path = os.path.join(
            tht_root, 'overcloud-resource-registry-puppet.yaml')
        environments.insert(0, resource_registry_path)

        # use deployed-server because we run os-collect-config locally
        deployed_server_env = os.path.join(
            tht_root, 'environments',
            'deployed-server-environment.yaml')
        environments.append(deployed_server_env)

        undercloud_yaml = os.path.join(tht_root, 'undercloud.yaml')
        return self._heat_deploy(parsed_args.stack, undercloud_yaml,
                                 parameters, environments, parsed_args.timeout,
                                 parsed_args.heat_api_port,
                                 parsed_args.fake_keystone_port)

    def get_parser(self, prog_name):
        parser = argparse.ArgumentParser(
            description=self.get_description(),
            prog=prog_name,
            add_help=False
        )
        parser.add_argument(
            '--templates', nargs='?', const=constants.TRIPLEO_HEAT_TEMPLATES,
            help=_("The directory containing the Heat templates to deploy"),
        )
        parser.add_argument('--stack',
                            help=_("Stack name to create"),
                            default='undercloud')
        parser.add_argument('-t', '--timeout', metavar='<TIMEOUT>',
                            type=int, default=30,
                            help=_('Deployment timeout in minutes.'))

        parser.add_argument(
            '-e', '--environment-file', metavar='<HEAT ENVIRONMENT FILE>',
            action='append', dest='environment_files',
            help=_('Environment files to be passed to the heat stack-create '
                   'or heat stack-update command. (Can be specified more than '
                   'once.)')
        )
        parser.add_argument(
            '--environment-directory', metavar='<HEAT ENVIRONMENT DIRECTORY>',
            action='append', dest='environment_directories',
            default=[os.path.join(os.environ.get('HOME', ''), '.tripleo',
                     'environments')],
            help=_('Environment file directories that are automatically '
                   ' added to the heat stack-create or heat stack-update'
                   ' commands. Can be specified more than once. Files in'
                   ' directories are loaded in ascending sort order.')
        )
        parser.add_argument(
            '--heat-api-paste-ini', metavar='<HEAT_API_PASTE_INI>',
            dest='heat_api_paste_ini',
            default=self._default_api_paste_ini(),
            help=_('Location of the heat api-paste.ini file to use when '
                   'starting Heat to drive the installation. Optional: '
                   'Default locations will be searched if not specified.)')
        )
        parser.add_argument(
            '--heat-api-port', metavar='<HEAT_API_PORT>',
            dest='heat_api_port',
            default='8006',
            help=_('Heat API port to use for the installers private'
                   ' Heat API instance. Optional. Default: 8006.)')
        )
        parser.add_argument(
            '--fake-keystone-port', metavar='<FAKE_KEYSTONE_PORT>',
            dest='fake_keystone_port',
            default='35358',
            help=_('Keystone API port to use for the installers private'
                   ' fake Keystone API instance. Optional. Default: 35358.)')
        )

        return parser

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)" % parsed_args)

        if not parsed_args.heat_api_paste_ini:
            print('Unable to find the heat api-paste.ini file. Please set '
                  '--heat-api-paste-ini to point to its location.')
            return

        self._install_base()

        # NOTE(dprince): It would be nice if heat supported true 'noauth'
        # use in a local format for our use case here (or perhaps dev testing)
        # but until it does running our own lightweight shim to mock out
        # the required API calls works just as well. To keep fake keystone
        # light we run it in a thread.
        if not os.environ.get('FAKE_KEYSTONE_PORT'):
            os.environ['FAKE_KEYSTONE_PORT'] = parsed_args.fake_keystone_port
        eventlet.spawn(fake_keystone.launch)

        handle, sqlite_db_file = tempfile.mkstemp()
        os.close(handle)

        heat_conf = self._create_heat_config(sqlite_db_file,
                                             parsed_args.heat_api_paste_ini,
                                             parsed_args.heat_api_port,
                                             parsed_args.fake_keystone_port)
        self._heat_db_sync(heat_conf)

        pid = None
        try:
            pid = os.fork()
            if pid == 0:
                # NOTE(dprince): we launch heat with fork exec because
                # we don't want it to inherit our args. Launching heat
                # as a "library" would be cool... but that would require
                # more refactoring. It runs a single process < 100MB and
                # we kill it always below.
                self._launch_local_heat(heat_conf)
            else:
                self._wait_local_port_ready(parsed_args.fake_keystone_port)
                self._wait_local_port_ready(parsed_args.heat_api_port)

                if self._deploy_tripleo_heat_templates(parsed_args):
                    print("\nUndercloud Deploy Successful.")
                else:
                    raise exceptions.DeploymentError("Stack create failed.")

        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)
            if os.path.exists(sqlite_db_file):
                os.remove(sqlite_db_file)
