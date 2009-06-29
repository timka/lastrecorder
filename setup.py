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

import os
from setuptools import setup, find_packages

name = 'LastRecorder'
module = name.lower()
execfile(os.path.join(module, 'release.py'))

setup(
    name = name, 
    version = version,
    packages = find_packages(),
    entry_points = dict(
        console_scripts = [
            '%s-cli = %s.main:cli_main' % (module, module),
        ],
        gui_scripts = [
            '%s = %s.gui:gui_main' % (module, module),
        ],
    ),
    data_files = [
        (os.path.join('share', 'applications'), ['%s.desktop' % module]),
        (os.path.join('share', 'pixmaps'), ['%s.png' % module]),
        (os.path.join('share', module), ['%s.glade' % module]),
    ],
)
