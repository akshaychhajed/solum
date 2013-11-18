# -*- coding: utf-8 -*-
#
# Copyright 2013 - Noorul Islam K M
#
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

import pecan
from wsme import types as wtypes
import wsmeext.pecan as wsme_pecan

from solum.api.controllers import v1


STATUS_KIND = wtypes.Enum(str, 'SUPPORTED', 'CURRENT', 'DEPRECATED')


class Link(wtypes.Base):
    """A link representation."""

    href = wtypes.text
    "The link url"

    targetName = wtypes.text
    "Textual name of the target link"

    @classmethod
    def sample(cls):
        return cls(href=('http://localhost:9777/v1'),
                   targetName='v1')


class Version(wtypes.Base):
    """Version representation."""

    id = wtypes.text
    "The version identifier"

    status = STATUS_KIND
    "The status of the API (SUPPORTED, CURRENT or DEPRECATED)"

    link = Link
    "The link to the versioned API"

    @classmethod
    def sample(cls):
        return cls(id='v1.0',
                   status='CURRENT',
                   link=Link(targetName='v1',
                             href='http://localhost:9777/v1'))


class RootController(object):

    v1 = v1.Controller()

    @wsme_pecan.wsexpose([Version])
    def index(self):
        host_url = '%s/%s' % (pecan.request.host_url, 'v1')
        v1 = Version(id='v1.0',
                     status='CURRENT',
                     link=Link(targetName='v1',
                               href=host_url))
        return [v1]
