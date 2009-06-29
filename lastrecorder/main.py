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

'''
  %prog [options] URL [URL [...]]
  OR
  %prog --help

Features:
  * <artist>/<album>/<title> file naming scheme
  * Automatic ID3 tags
  * Skipping previously recorded tracks (optional)
  * Stripping Windows-incompatible characters and/or spaces from paths
    (optional)
  * lastfm:// URL quoting (optional)

Examples:
  %prog lastfm://usertags/liago0sh/positive
  %prog -s "lastfm://usertags/liago0sh/heavy electro" -d
'''
import os
import sys
import socket
import httplib
import logging
import logging.handlers

from optparse import OptionParser
from getpass import getpass

from lastrecorder import util
from lastrecorder.radio import RadioClient, HandshakeError, setup_urllib2
from lastrecorder.config import Config
from lastrecorder import LOGFILE, IS_WINDOWS, DOTDIR, DEFAULTS


def parse_args(config, defaults):
    parser = OptionParser(usage=__doc__.rstrip())

    defaults.update((k, v) for k, v in config if v is not None)
    parser.set_defaults(**defaults)

    parser.add_option('--output', '-o', dest='outdir', action='store',
                      help=('override default output directory for mp3 files.'
                            ' Will be created if does not exist'
                            ' [default: %s]') % defaults['outdir'])
    parser.add_option('--no-gui', '-g', dest='gui',
                      action='store_false', help="don't use GUI")
    parser.add_option('--username', '-u', dest='username', action='store',
                      help='Last.fm user to login as')
    parser.add_option('--passwordmd5', '-p', dest='passwordmd5', action='store',
                      help='Last.fm password MD5 hex digest')
    parser.add_option('--debug', '-d', dest='debug', action='store_true',
                      help='verbose log messages')
    parser.add_option('--no-save-credentials', '-n', dest='save',
                      action='store_false',
                      help="don't save Last.fm user and encrypted password")
    parser.add_option('--no-quote', '-q', dest='quote', action='store_false',
                      help="don't quote lastfm:// URL's")
    parser.add_option('--no-skip-existing', '-e', dest='skip_existing',
                      action='store_false',
                      help="don't skip tracks that have already been recorded")
    parser.add_option('--no-strip-windows-incompat', '-w',
                      action='store_false',
                      dest='strip_windows_incompat',
                      help=('do not strip Windows-incompatible characters'
                            ' from file names'))
    parser.add_option('--no-strip-spaces', '-s', dest='strip_spaces',
                      action='store_false',
                      help="don't replace space characters with underscores")
    options, args = parser.parse_args()

    # Quote URLs
    if options.quote:
        args = [ util.quote_url(arg) for arg in args ]

    return parser, options, args


def setup_logging(options):
    level = logging.INFO
    if options.debug:
        level = logging.DEBUG
    if os.path.exists(LOGFILE):
        os.unlink(LOGFILE)
    handler = logging.handlers.RotatingFileHandler(LOGFILE, 'w',
                                                   10 * 1024 * 1024, 1)
    handler.setLevel(logging.DEBUG)
    format = '%(asctime)s %(levelname)8s %(name)s: %(message)s'
    formatter = logging.Formatter(format)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    # Console logger
    if not IS_WINDOWS:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(formatter)
        root.addHandler(handler)


def progress_cb(track, position, length):
    percent = float(position) / float(length) * 100
    msg = '    %d/%d (%0.2f%%)\r'
    msg = msg % (position, length, percent)
    sys.stderr.write(msg)


def setup(defaults):
    reload(sys).setdefaultencoding('utf-8')

    if not os.path.exists(DOTDIR):
        os.makedirs(DOTDIR)

    config = Config()
    config.parse()

    parser, options, urls = parse_args(config, defaults)
    setup_logging(options)
    log = logging.getLogger('setup')

    try:
        import pygtk
    except ImportError:
        pygtk = None
    try:
        import mutagen
    except ImportError:
        mutagen = None

    if options.gui and not pygtk:
        options.gui = False
        log = logging.getLogger('main')
        log.warn('pygtk library not found. GUI disabled.')
    if not mutagen:
        log.warn('mutagen library not found. Tagging disabled.')
    if not options.gui and not urls:
        parser.error('Please specify lastfm:// URL')
    if not os.path.exists(options.outdir):
        os.makedirs(options.outdir)

    setup_urllib2()

    return config, options, urls


def getpassword(username):
    password = None
    while not password:
        password = getpass.getpass('%s Last.fm password: ' % username)
    return password


def get_credentials(options, config):
    log = logging.getLogger('main')
    passwordmd5 = None
    username = options.username
    if not username and not config.username:
        log.debug('username not found in config')
        while True:
            username = raw_input('Last.fm user: ')
            if not username:
                sys.stderr.write('Please enter Last.fm user name\n')
            else:
                break
    passwordmd5 = config.passwordmd5
    if not passwordmd5:
        passwordmd5 = util.md5(getpassword(username)).hexdigest()

    assert username, "No username specified"
    assert passwordmd5, "No password specified"

    return username, passwordmd5


def cli_main(config=None, options=None, urls=None):
    if config is None:
        DEFAULTS['gui'] = False
        config, options, urls = setup(DEFAULTS.copy())
    return main(config, options, urls)


def main(config=None, options=None, urls=None):
    if config is None:
        config, options, urls = setup(DEFAULTS.copy())

    log = logging.getLogger('main')
    try:
        if options.gui:
            try:
                from lastrecorder.gui import gui_main
            except ImportError:
                log.error('pygtk not found. Disabling GUI.')
                options.gui = False
            else:
                return gui_main(config, options, urls)

        username, passwordmd5 = get_credentials(options, config)

        if options.save:
            config.username = username
            config.passwordmd5 = passwordmd5
            try:
                config.write()
            except (IOError, OSError), e:
                log.exception('Error saving config file: %s', e)

        radio_client = RadioClient(username, passwordmd5, options.outdir,
                                   options.strip_windows_incompat,
                                   options.strip_spaces, options.skip_existing,
                                   progress_cb)
        try:
            radio_client.loop(urls)
        except HandshakeError, e:
            log.error('%s', e)
            return 1
        except (IOError, socket.error, httplib.HTTPException), e:
            log.exception('I/O or HTTP error: %s', e)
            return 1
        except ValueError, e:
            log.exception('Unexpected error: %s', e)
            return 1
        except KeyboardInterrupt:
            log.info('Interrupted. Exiting.')
            return
    except Exception, e:
        log.exception(e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
