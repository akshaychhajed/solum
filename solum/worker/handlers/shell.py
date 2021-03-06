# Copyright 2014 - Rackspace Hosting
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

"""Solum Worker shell handler."""

import ast
import base64
import os
import random
import shelve
import string
import subprocess

from oslo.config import cfg

import solum
from solum.common import clients
from solum.common import exception
from solum.common import repo_utils
from solum.common import solum_keystoneclient
from solum.conductor import api as conductor_api
from solum.deployer import api as deployer_api
from solum import objects
from solum.objects import assembly
from solum.objects import image
from solum.openstack.common import log as logging
from solum.openstack.common import uuidutils
import solum.uploaders.common as uploader_common
import solum.uploaders.local as local_uploader
import solum.uploaders.swift as swift_uploader


LOG = logging.getLogger(__name__)

ASSEMBLY_STATES = assembly.States
IMAGE_STATES = image.States

cfg.CONF.import_opt('task_log_dir', 'solum.worker.config', group='worker')
cfg.CONF.import_opt('proj_dir', 'solum.worker.config', group='worker')
cfg.CONF.import_opt('param_file_path', 'solum.worker.config', group='worker')
cfg.CONF.import_opt('log_upload_strategy', 'solum.worker.config',
                    group='worker')
cfg.CONF.import_opt('image_storage', 'solum.worker.config', group='worker')
cfg.CONF.import_opt('temp_url_secret', 'solum.worker.config', group='worker')
cfg.CONF.import_opt('temp_url_protocol', 'solum.worker.config', group='worker')
cfg.CONF.import_opt('temp_url_ttl', 'solum.worker.config', group='worker')


def upload_task_log(ctxt, original_path, resource, build_id, stage):
    strategy = cfg.CONF.worker.log_upload_strategy
    LOG.debug("User log upload strategy: %s" % strategy)

    uploader = {
        'local': local_uploader.LocalStorage,
        'swift': swift_uploader.SwiftUpload,
    }.get(strategy, uploader_common.UploaderBase)
    uploader(ctxt, original_path, resource, build_id, stage).upload_log()


def job_update_notification(ctxt, build_id, state=None, description=None,
                            created_image_id=None, assembly_id=None):
    """send a status update to the conductor."""
    LOG.debug('build id:%s %s (%s) %s %s' % (build_id, state, description,
                                             created_image_id, assembly_id),
              context=solum.TLS.trace)
    conductor_api.API(context=ctxt).build_job_update(build_id, state,
                                                     description,
                                                     created_image_id,
                                                     assembly_id)


def get_assembly_by_id(ctxt, assembly_id):
    return solum.objects.registry.Assembly.get_by_id(ctxt, assembly_id)


def get_image_by_id(ctxt, image_id):
    return solum.objects.registry.Image.get_by_id(ctxt, image_id)


def get_parameter_by_assem_id(ctxt, assembly_id):
    assem = get_assembly_by_id(ctxt, assembly_id)
    param_obj = solum.objects.registry.Parameter.get_by_plan_id(ctxt,
                                                                assem.plan_id)
    return param_obj


def update_assembly_status(ctxt, assembly_id, status):
    if assembly_id is None:
        return
    LOG.debug('Updating assembly %s status to %s' % (assembly_id, status))
    data = {'status': status}
    conductor_api.API(context=ctxt).update_assembly(assembly_id, data)


def update_lp_status(ctxt, image_id, status, external_ref=None):
    if image_id is None:
        return
    LOG.debug('Updating languagepack %s status to %s and external_ref to %s'
              % (image_id, status, external_ref))
    conductor_api.API(context=ctxt).update_image(image_id, status,
                                                 external_ref)


class Handler(object):
    def echo(self, ctxt, message):
        LOG.debug("%s" % message)

    @exception.wrap_keystone_exception
    def _get_environment(self, ctxt, source_uri, assembly_id=None,
                         test_cmd=None, run_cmd=None):
        # create a minimal environment
        user_env = {}

        for var in ['PATH', 'LOGNAME', 'LANG', 'HOME', 'USER', 'TERM']:
            if var in os.environ:
                user_env[var] = os.environ[var]

        if assembly_id is not None:
            assem = get_assembly_by_id(ctxt, assembly_id)
            user_env['ASSEMBLY_ID'] = str(assem.uuid)
        else:
            str_assem = (''.join(random.choice(string.ascii_uppercase)
                         for i in range(10)))
            user_env['ASSEMBLY_ID'] = str_assem

        user_env['IMAGE_STORAGE'] = cfg.CONF.worker.image_storage
        user_env['DELETE_LOCAL_CACHE'] = cfg.CONF.worker.delete_local_cache

        if cfg.CONF.worker.image_storage == 'docker_registry':
            if cfg.CONF.worker.docker_reg_endpoint is None:
                LOG.debug("DU upload set to docker registry,")
                LOG.debug("but docker registry endpoint is not set.")
                LOG.debug("Setting it to 127.0.0.1")
                cfg.CONF.worker.docker_reg_endpoint = '127.0.0.1'
            user_env['DOCKER_REGISTRY'] = cfg.CONF.worker.docker_reg_endpoint
        else:
            client_region_name = clients.get_client_option('swift',
                                                           'region_name')
            user_env['OS_AUTH_TOKEN'] = ctxt.auth_token
            user_env['OS_AUTH_URL'] = ctxt.auth_url or ''
            user_env['OS_REGION_NAME'] = client_region_name
            kc = solum_keystoneclient.KeystoneClientV3(ctxt)
            user_env['OS_IMAGE_URL'] = kc.client.service_catalog.url_for(
                service_type='image',
                endpoint_type='publicURL')
            user_env['OS_STORAGE_URL'] = kc.client.service_catalog.url_for(
                service_type='object-store',
                endpoint_type='publicURL',
                region_name=client_region_name)
            user_env['TEMP_URL_SECRET'] = cfg.CONF.worker.temp_url_secret
            user_env['TEMP_URL_PROTOCOL'] = cfg.CONF.worker.temp_url_protocol
            user_env['TEMP_URL_TTL'] = cfg.CONF.worker.temp_url_ttl

        if test_cmd is not None:
            user_env['TEST_CMD'] = test_cmd
        if run_cmd is not None:
            user_env['RUN_CMD'] = run_cmd

        user_env['PROJECT_ID'] = ctxt.tenant

        user_env['BUILD_ID'] = uuidutils.generate_uuid()
        user_env['SOLUM_TASK_DIR'] = cfg.CONF.worker.task_log_dir

        params_env = self._get_parameter_env(ctxt, source_uri, assembly_id,
                                             user_env['BUILD_ID'])
        user_env.update(params_env)
        return user_env

    @property
    def proj_dir(self):
        if cfg.CONF.worker.proj_dir:
            return cfg.CONF.worker.proj_dir
        return os.path.abspath(os.path.join(os.path.dirname(__file__),
                                            '..', '..', '..'))

    def _get_build_command(self, ctxt, stage, source_uri, name,
                           base_image_id, source_format, image_format,
                           commit_sha, artifact_type=None, lp_name=None):

        # map the input formats to script paths.
        # TODO(asalkeld) we need an "auto".
        pathm = {'heroku': 'lp-cedarish',
                 'dib': 'diskimage-builder',
                 'dockerfile': 'lp-dockerfile',
                 'chef': 'lp-chef',
                 'docker': 'docker',
                 'qcow2': 'docker',
                 'vm': 'docker'}
        if base_image_id == 'auto' and image_format == 'qcow2':
            base_image_id = 'cedarish'
        build_app_path = os.path.join(self.proj_dir, 'contrib',
                                      pathm.get(source_format, 'lp-cedarish'),
                                      pathm.get(image_format, 'docker'))

        if artifact_type == 'language_pack':
            build_lp = os.path.join(build_app_path, 'build-lp')
            return [build_lp, source_uri, name, ctxt.tenant]

        if stage == 'unittest':
            build_app = os.path.join(build_app_path, 'unittest-app')
            return [build_app, source_uri, commit_sha, ctxt.tenant,
                    base_image_id, lp_name]
        elif stage == 'build':
            build_app = os.path.join(build_app_path, 'build-app')
            return [build_app, source_uri, name, ctxt.tenant, base_image_id,
                    lp_name]

    def _get_parameter_env(self, ctxt, source_uri, assembly_id, build_id):
        param_env = {}
        if assembly_id is None:
            return param_env

        param_obj = get_parameter_by_assem_id(ctxt, assembly_id)
        if param_obj is None:
            return param_env

        user_param_file = '/'.join([cfg.CONF.worker.param_file_path,
                                    build_id, 'user_params'])
        solum_param_file = '/'.join([cfg.CONF.worker.param_file_path,
                                     build_id, 'solum_params'])
        try:
            os.makedirs(os.path.dirname(user_param_file), 0o700)
        except OSError as ex:
            LOG.error("Error creating dirs to write out param files, %s" % ex)
            return param_env

        def _sanitize_param(s):
            if s is None:
                return ''
            elif type(s) in [str, unicode]:
                # Handles the case of exporting a var with a multi-line string
                return ''.join(['"', s.strip('\n').replace('"', '\\"'), '"'])
            else:
                return str(s)

        with open(user_param_file, 'w') as f:
            f.write("#!/bin/bash\n")
            if param_obj.user_defined_params:
                for k, v in param_obj.user_defined_params.items():
                    if k and k.startswith('_SYSTEM'):
                        # Variables for control purpose, e.g. _SYSTEM_USE_DRONE
                        param_env[k] = _sanitize_param(v)
                    else:
                        f.write("export %s=%s\n" % (k, _sanitize_param(v)))
        with open(solum_param_file, 'w') as f:
            f.write("#!/bin/bash\n")
            if param_obj.sys_defined_params:
                for k, v in param_obj.sys_defined_params.items():
                    if k == 'REPO_DEPLOY_KEYS':
                        # Pass in deploy key as an environment variable
                        param_env[k] = self._get_private_key(v, source_uri)
                    f.write("export %s=%s\n" % (k, _sanitize_param(v)))

        param_env['USER_PARAMS'] = user_param_file
        param_env['SOLUM_PARAMS'] = solum_param_file
        return param_env

    def launch_workflow(self, ctxt, build_id, git_info, ports, name,
                        base_image_id, source_format, image_format,
                        assembly_id, workflow, test_cmd, run_cmd):
        if 'unittest' in workflow:
            if self._do_unittest(ctxt, build_id, git_info, name, base_image_id,
                                 source_format, image_format, assembly_id,
                                 test_cmd) != 0:
                return

        built_image = None
        if 'build' in workflow:
            built_image = self._do_build(ctxt, build_id, git_info, name,
                                         base_image_id, source_format,
                                         image_format, assembly_id,
                                         run_cmd)

        if 'deploy' in workflow and built_image:
            self._do_deploy(ctxt, assembly_id, ports, built_image)

    def build(self, ctxt, build_id, git_info, name, base_image_id,
              source_format, image_format, assembly_id, run_cmd):
        self._do_build(ctxt, build_id, git_info, name, base_image_id,
                       source_format, image_format, assembly_id, run_cmd)

    def unittest(self, ctxt, build_id, git_info, name, base_image_id,
                 source_format, image_format, assembly_id, test_cmd):
        self._do_unittest(ctxt, build_id, git_info, name, base_image_id,
                          source_format, image_format, assembly_id, test_cmd)

    def _do_deploy(self, ctxt, assembly_id, ports, built_image):
        deployer_api.API(context=ctxt).deploy(assembly_id=assembly_id,
                                              image_id=built_image,
                                              ports=ports)

    def _do_build(self, ctxt, build_id, git_info, name, base_image_id,
                  source_format, image_format, assembly_id, run_cmd):
        update_assembly_status(ctxt, assembly_id, ASSEMBLY_STATES.BUILDING)

        solum.TLS.trace.clear()
        solum.TLS.trace.import_context(ctxt)

        source_uri = git_info['source_url']

        lp_name = ''
        if base_image_id != 'auto':
            image = objects.registry.Image.get_lp_by_name_or_uuid(
                ctxt, base_image_id)
            base_image_id = image.external_ref
            lp_name = image.name

        build_cmd = self._get_build_command(ctxt, 'build', source_uri,
                                            name, base_image_id,
                                            source_format, image_format, '',
                                            lp_name=lp_name)
        solum.TLS.trace.support_info(build_cmd=' '.join(build_cmd),
                                     assembly_id=assembly_id)

        try:
            user_env = self._get_environment(ctxt, source_uri,
                                             assembly_id=assembly_id,
                                             run_cmd=run_cmd)
        except exception.SolumException as env_ex:
            LOG.exception(env_ex)
            job_update_notification(ctxt, build_id, IMAGE_STATES.ERROR,
                                    description=str(env_ex),
                                    assembly_id=assembly_id)

        log_env = user_env.copy()
        if 'OS_AUTH_TOKEN' in log_env:
            del log_env['OS_AUTH_TOKEN']
        solum.TLS.trace.support_info(environment=log_env)

        job_update_notification(ctxt, build_id, IMAGE_STATES.BUILDING,
                                description='Starting the image build',
                                assembly_id=assembly_id)
        # TODO(datsun180b): Associate log with assembly properly
        logpath = "%s/%s-%s.log" % (user_env['SOLUM_TASK_DIR'],
                                    'build',
                                    user_env['BUILD_ID'])
        LOG.debug("Build logs stored at %s" % logpath)
        out = None
        assem = None
        if assembly_id is not None:
            assem = get_assembly_by_id(ctxt, assembly_id)
            if assem.status == ASSEMBLY_STATES.DELETING:
                return

        try:
            out = subprocess.Popen(build_cmd,
                                   env=user_env,
                                   stdout=subprocess.PIPE).communicate()[0]
        except (OSError, ValueError) as subex:
            LOG.exception(subex)
            job_update_notification(ctxt, build_id, IMAGE_STATES.ERROR,
                                    description=str(subex),
                                    assembly_id=assembly_id)
            update_assembly_status(ctxt, assembly_id, ASSEMBLY_STATES.ERROR)
            return

        if assem is not None:
            assem.type = 'app'
            upload_task_log(ctxt, logpath, assem, user_env['BUILD_ID'],
                            'build')

        # we expect one line in the output that looks like:
        # created_image_id=<id of the image>
        # If image_storage is 'glance', this will be glance_id
        # If image_storage is 'docker_registry', this will be URL:PORT/APP
        # If image_storage is 'swift', this will be swift tempUrl for the DU
        created_image_id = None

        for line in out.split('\n'):
            if 'created_image_id' in line:
                solum.TLS.trace.support_info(build_out_line=line)
                # (devkulkarni): When the image_storage is swift, the form
                # of the tempUrl is like so:
                # https://<>/<app-name>?temp_url_sig=val&temp_url_expires=val
                # Because of the presence of the query string, we cannot use
                # splitting on '=' like we do for others. Hence the special
                # logic below.
                if cfg.CONF.worker.image_storage == 'swift':
                    img_id = line.replace("created_image_id=", '')
                    # (devkulkarni): We need the APP_NAME in deployer
                    # Appending it to the created_image_id for now (below).
                    # TODO(devkulkarni): Store the APP_NAME in assembly
                    # in deployer
                    img_id += "APP_NAME=" + name
                    created_image_id = img_id
                else:
                    created_image_id = line.split('=')[-1].strip()
                LOG.debug("created_image_id:%s" % created_image_id)
        if not created_image_id:
            job_update_notification(ctxt, build_id, IMAGE_STATES.ERROR,
                                    description='image not created',
                                    assembly_id=assembly_id)
            update_assembly_status(ctxt, assembly_id, ASSEMBLY_STATES.ERROR)
            return
        else:
            job_update_notification(ctxt, build_id, IMAGE_STATES.READY,
                                    description='built successfully',
                                    created_image_id=created_image_id,
                                    assembly_id=assembly_id)
            update_assembly_status(ctxt, assembly_id, ASSEMBLY_STATES.BUILT)
            return created_image_id

    def _do_unittest(self, ctxt, build_id, git_info, name, base_image_id,
                     source_format, image_format, assembly_id, test_cmd):
        if test_cmd is None:
            LOG.debug("Unit test command is None; skipping unittests.")
            return 0

        commit_sha = git_info.get('commit_sha', '')
        status_url = git_info.get('status_url')
        repo_token = git_info.get('repo_token')

        LOG.debug("Running unittests.")
        update_assembly_status(ctxt, assembly_id, ASSEMBLY_STATES.UNIT_TESTING)

        lp_name = ''
        if base_image_id != 'auto':
            image = objects.registry.Image.get_lp_by_name_or_uuid(
                ctxt, base_image_id)
            base_image_id = image.external_ref
            lp_name = image.name

        git_url = git_info['source_url']
        command = self._get_build_command(ctxt, 'unittest', git_url, name,
                                          base_image_id,
                                          source_format, image_format,
                                          commit_sha, lp_name=lp_name)

        solum.TLS.trace.clear()
        solum.TLS.trace.import_context(ctxt)

        user_env = self._get_environment(ctxt, git_url,
                                         assembly_id=assembly_id,
                                         test_cmd=test_cmd)
        log_env = user_env.copy()
        if 'OS_AUTH_TOKEN' in log_env:
            del log_env['OS_AUTH_TOKEN']
        solum.TLS.trace.support_info(environment=log_env)

        logpath = "%s/%s-%s.log" % (user_env['SOLUM_TASK_DIR'],
                                    'unittest',
                                    user_env['BUILD_ID'])
        LOG.debug("Unittest logs stored at %s" % logpath)

        returncode = -1
        assem = None
        if assembly_id is not None:
            assem = get_assembly_by_id(ctxt, assembly_id)
            if assem.status == ASSEMBLY_STATES.DELETING:
                return returncode

        try:
            runtest = subprocess.Popen(command, env=user_env,
                                       stdout=subprocess.PIPE)
            returncode = runtest.wait()
        except OSError as subex:
            LOG.exception("Exception running unit tests:")
            LOG.exception(subex)

        if assem is not None:
            assem.type = 'app'
            upload_task_log(ctxt, logpath, assem, user_env['BUILD_ID'],
                            'unittest')

        if returncode == 0:
            update_assembly_status(ctxt, assembly_id,
                                   ASSEMBLY_STATES.UNIT_TESTING_PASSED)
        elif returncode > 0:
            LOG.error("Unit tests failed. Return code is %r" % (returncode))
            update_assembly_status(ctxt, assembly_id,
                                   ASSEMBLY_STATES.UNIT_TESTING_FAILED)
        elif returncode < 0:
            LOG.error("Error running unit tests.")
            update_assembly_status(ctxt, assembly_id, ASSEMBLY_STATES.ERROR)

        repo_utils.send_status(returncode, status_url, repo_token)

        return returncode

    def _get_private_key(self, source_creds, source_url):
        source_private_key = ''
        if source_creds:
            cfg.CONF.import_opt('system_param_store',
                                'solum.api.handlers.plan_handler',
                                group='api')
            store = cfg.CONF.api.system_param_store

            if store == 'database':
                deploy_keys_str = base64.b64decode(source_creds)
            elif store == 'barbican':
                client = clients.OpenStackClients(None).barbican().admin_client
                secret = client.secrets.get(secret_ref=source_creds)
                deploy_keys_str = secret.payload
            elif store == 'local_file':
                cfg.CONF.import_opt('system_param_file',
                                    'solum.api.handlers.plan_handler',
                                    group='api')
                secrets_file = cfg.CONF.api.system_param_file
                s = shelve.open(secrets_file)
                deploy_keys_str = s[str(source_creds)]
                deploy_keys_str = base64.b64decode(deploy_keys_str)
                s.close()
            deploy_keys = ast.literal_eval(deploy_keys_str)
            for dk in deploy_keys:
                if source_url == dk['source_url']:
                    source_private_key = dk['private_key']
        return source_private_key

    def build_lp(self, ctxt, image_id, git_info, name, source_format,
                 image_format, artifact_type):
        update_lp_status(ctxt, image_id, IMAGE_STATES.BUILDING)

        solum.TLS.trace.clear()
        solum.TLS.trace.import_context(ctxt)

        source_uri = git_info['source_url']
        build_cmd = self._get_build_command(ctxt, 'build', source_uri,
                                            name, str(image_id),
                                            source_format, 'docker', '',
                                            artifact_type)

        try:
            user_env = self._get_environment(ctxt, source_uri)
        except exception.SolumException as env_ex:
            LOG.exception(_("Failed to successfully get environment for "
                            "building languagepack: `%s`"),
                          image_id)
            LOG.exception(env_ex)

        log_env = user_env.copy()
        if 'OS_AUTH_TOKEN' in log_env:
            del log_env['OS_AUTH_TOKEN']
        solum.TLS.trace.support_info(environment=log_env)

        logpath = "%s/%s-%s.log" % (user_env['SOLUM_TASK_DIR'],
                                    'languagepack',
                                    user_env['BUILD_ID'])
        LOG.debug("Languagepack logs stored at %s" % logpath)

        out = None
        status = IMAGE_STATES.ERROR
        image_external_ref = None

        try:
            out = subprocess.Popen(build_cmd,
                                   env=user_env,
                                   stdout=subprocess.PIPE).communicate()[0]

            # we expect one line in the output that looks like:
            # image_external_ref=<external storage ref>

            for line in out.split('\n'):
                if 'image_external_ref' in line:
                    solum.TLS.trace.support_info(build_lp_out_line=line)
                    # When the image_storage is swift,
                    # we cannot use splitting on '=' like we do for others.
                    # Hence the special logic below.
                    if cfg.CONF.worker.image_storage == 'swift':
                        img_id = line.replace("image_external_ref=", '')
                        image_external_ref = img_id
                    else:
                        image_external_ref = line.split('=')[-1].strip()
                    break
            if image_external_ref is not None:
                status = IMAGE_STATES.READY
            else:
                status = IMAGE_STATES.ERROR
        except OSError as subex:

            LOG.exception(_("Failed to successfully build languagepack: `%s`"),
                          image_id)
            LOG.exception(subex)

        img = get_image_by_id(ctxt, image_id)
        img.type = 'languagepack'
        upload_task_log(ctxt, logpath, img,
                        user_env['BUILD_ID'], 'languagepack')
        update_lp_status(ctxt, image_id, status, image_external_ref)
