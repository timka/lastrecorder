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
