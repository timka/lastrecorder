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
import sys

__all__ = ['NAME', 'DOTDIR', 'MUSICDIR', 'LOGFILE', 'IS_WINDOWS', 'DEFAULTS']

NAME = 'lastrecorder'
DOTDIR = os.path.join(os.path.expanduser('~'), '.%s' % NAME)
MUSICDIR = os.path.join(DOTDIR, 'music')
LOGFILE = os.path.join(DOTDIR, '%s.log' % NAME)
IS_WINDOWS = sys.platform.lower().startswith('win')
DEFAULTS = dict(save=True, debug=False, quote=True, skip_existing=True,
                strip_windows_incompat=True, strip_spaces=True,
                outdir=MUSICDIR, gui=True)
