# -*- coding: utf-8 -*-
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import with_statement
import os
from ConfigParser import SafeConfigParser, NoOptionError, NoSectionError

from lastrecorder import CONFIGDIR, NAME

class Config(object):
    filename = os.path.join(CONFIGDIR, '%s.cfg' % NAME)

    class __metaclass__(type):
        bool_vars = ['strip_windows_incompat', 'strip_spaces',
                        'skip_existing', 'save', 'debug']
        login_vars = ['username', 'passwordmd5']
        str_vars = ['outdir', 'station', 'station_type']

        def __new__(mcls, name, bases, namespace):
            for option in mcls.bool_vars:
                get, set, delete = mcls.make_accessors(option, 'options',
                                                       'getboolean')
                namespace[option] = property(get, set, delete)
            for option in mcls.login_vars:
                get, set, delete = mcls.make_accessors(option, 'lastfm_user',
                                                       'get')
                namespace[option] = property(get, set, delete)
            for option in mcls.str_vars:
                get, set, delete = mcls.make_accessors(option, 'options', 'get')
                namespace[option] = property(get, set, delete)
            namespace['vars'] = (mcls.bool_vars + mcls.login_vars +
                                 mcls.str_vars)
            return type.__new__(mcls, name, bases, namespace)

        @staticmethod
        def make_accessors(name, section, method):
            def getter(self):
                get = getattr(self.parser, method)
                try:
                    return get(section, name)
                except (NoOptionError, NoSectionError):
                    return
            def setter(self, value):
                if not self.parser.has_section(section):
                    self.parser.add_section(section)
                try:
                    self.parser.set(section, name, str(value))
                except (NoOptionError):
                    return
            def remover(self):
                try:
                    return self.parser.remove_option(section, name)
                except (NoOptionError, NoSectionError):
                    return

            return getter, setter, remover

    def __init__(self):
        self.parser = SafeConfigParser()
        self.parsed = False

    def parse(self):
        self.parsed = True
        if not os.path.exists(self.filename):
            return
        if not self.parser.read(self.filename):
            return

    def write(self):
        with open(self.filename, 'w') as fp:
            self.parser.write(fp)
            os.fsync(fp.fileno())

    def __iter__(self):
        return iter([ (var, getattr(self, var)) for var in self.vars ])

    def clear_password(self):
        try:
            self.parser.remove_option('lastfm_user', 'passwordmd5')
        except (NoOptionError, NoSectionError):
            return
