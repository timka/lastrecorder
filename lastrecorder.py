#!/usr/bin/env python
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
from __future__ import with_statement

import atexit
import codecs
import encodings.utf_8
import errno
import getpass
import httplib
import locale
import logging
import logging.handlers
import os
import select
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import urllib2
import xml.sax

try:
    from hashlib import md5
except ImportError:
    from md5 import md5
try:
    import mutagen
except ImportError:
    mutagen = None
try:
    import pygtk
except ImportError:
    pygtk = None
else:
    pygtk.require("2.0")
    import gobject
    from gobject import idle_add
    import gtk
    gtk.gdk.threads_init()


from optparse import OptionParser
from ConfigParser import SafeConfigParser, NoOptionError, NoSectionError
from pprint import pformat

NAME = 'lastrecorder'
DOTDIR = os.path.join(os.path.expanduser('~'), '.%s' % NAME)
MUSICDIR = os.path.join(DOTDIR, 'music')
LOGFILE = os.path.join(DOTDIR, '%s.log' % NAME)
SOCKET_READ_SIZE = 512
SOCKET_TIMEOUT = 30
# Pretend to be Last.fm player
VERSION = '1.5.1.31879'
USER_AGENT = 'User-Agent: Last.fm Client %s (X11)' % VERSION

IS_WINDOWS = sys.platform.lower().startswith('win')
DEFAULTS = dict(save=True, debug=False, quote=True, skip_existing=True,
                strip_windows_incompat=True, strip_spaces=True,
                outdir=MUSICDIR, gui=True)


class Error(Exception):
    pass

class HandshakeError(Error):
    pass

class SessionError(Error):
    pass

class AdjustError(Error):
    pass


class InvalidURL(Error):
    message = 'Invalid Last.fm URL: %s'

    def __init__(self, url):
        self.url = url

    def __str__(self):
        return self.message % self.url


class NoContentAvailable(Error):
    pass


class BackoffDelay(object):
    def __init__(self, mult=4):
        self.mult = mult
        self.count = 0

    def sleep(self):
        seconds = self.mult * self.count * self.count
        log = logging.getLogger('BackoffDelay')
        log.info('Sleeping %s seconds', seconds)
        time.sleep(seconds)
        self.count += 1

    def reset(self):
        self.count = 0


class Track(dict):
    def __init__(self, *args, **kw):
        super(Track, self).__init__(*args, **kw)
        self.log = logging.getLogger(self.__class__.__name__)
        if not self.get('location'):
            raise ValueError('Bad track data: no stream location defined')
        default = '[unknown]'
        for key in 'title album creator'.split():
            if not self.get(key):
                self[key] = default
        self['artist'] = self['creator']

    def strip_windows_incompat(self, string, substitute='_'):
        '''Strip Windows-incompatible characters.
        '''
        s = substitute
        string = ''.join([ ((c in '\/:*?;"<>|' and s) or c) for c in string ])
        # Remove trailnig dots and spaces
        # http://mail.python.org/pipermail/python-list/2005-July/330549.html
        if s.endswith('.') or s.endswith(' '):
            string[-1] = s
        return string

    def getpath(self, strip_windows_incompat=False, strip_spaces=False):
        '''Get relative path to track file based on its metadata
        <artist>/<album>/<title>
        '''
        sep = os.path.sep
        data = self.copy()
        for key in 'title album creator'.split():
            value = data[key]
            if strip_windows_incompat:
                value = self.strip_windows_incompat(value)
            if strip_spaces:
                value = value.replace(' ', '_') 
            value = value.replace(sep, '-')
            data[key] = value

        artist, album, title = data['creator'], data['album'], data['title']
        dirpath = os.path.join(artist, album)
        filepath = os.path.join(dirpath, '%s.mp3' % title)
        return filepath

    def make_filename(self):
        data = self.copy()
        sep = os.path.sep
        for key in 'title creator'.split():
            value = data[key]
            value = self.strip_windows_incompat(value)
            value = value.replace(' ', '_') 
            value = value.replace(sep, '-')
            data[key] = value

        artist, title = data['creator'], data['title']
        filepath = '%s_-_%s.mp3' % (artist, title)
        return filepath

    def find_existing(self, dir):
        '''Find existing files for this track checking all possible naming
        schemes. Returns first matched path
        '''
        # Get list of all possible binary combinations of given length
        l = 2
        masks = [ 1 << i - 1 for i in range(l, 0, -1) ]
        binary_combinations = [ map(lambda x: bool(x & i), masks)
                                for i in range(1 << l) ]
        for args in binary_combinations:
            path = os.path.join(dir, self.getpath(*args))
            if os.path.exists(path):
                return path
        return None

    @property
    def name(self):
        return '%(creator)s — %(title)s' % self


class XSPFHandler(xml.sax.ContentHandler):
    '''XSPF playilst parser. Saves tracks into ``self.tracks`` as ``dict``s 
    '''
    def __init__(self):
        self.depth = -1
        self.indent = '  '
        self.log = logging.getLogger(self.__class__.__name__)
        self.data = ''
        self.tracks = []
        self.track = None
        self.track_attr = None

    def startElement(self, name, attrs):
        self.depth += 1
        i = self.indent
        a = [ '%s=%s' % item for item in attrs.items()]
        self.log.debug('%s+%s %s', i * self.depth, name, a)
        if self.depth == 2 and name == 'track':
            self.track = dict()
        if self.depth == 3 and self.track is not None:
            self.track_attr = name
            self.track.setdefault(name, None)

    def characters(self, data):
        self.data += data

    def endElement(self, name):
        i = self.indent
        data = self.data.strip()
        self.log.debug('%s %s', i * self.depth, data)
        if self.depth == 3 and self.track_attr is not None:
            self.track[self.track_attr] = data
        if self.depth == 2 and name == 'track':
            self.tracks.append(self.track)
            self.track = None
        self.depth -= 1
        self.data = ''


class RadioClient(object):
    base_url = 'http://ws.audioscrobbler.com/radio'
    handshake_url = (base_url + '/handshake.php'
                     '?version=%s&platform=linux'
                     '&platformversion=Unix%%2FLinux&username=%s'
                     '&passwordmd5=%s')
    adjust_url = base_url + '/adjust.php?session=%s&url=%s&lang=en'
    xspf_url = base_url + '/xspf.php?sk=%s&discovery=%s&desktop=%s'

    def __init__(self, username=None, passwordmd5=None, outdir=None,
                 strip_windows_incompat=False, strip_spaces=False,
                 skip_existing=False, progress_cb=None):
        self.username = username
        self.passwordmd5 = passwordmd5
        self.outdir = outdir
        self.strip_windows_incompat = strip_windows_incompat
        self.strip_spaces = strip_spaces
        self.skip_existing = skip_existing
        if progress_cb is not None:
            self.progress_cb = progress_cb

        self.session = None
        self.station_name = None
        self.tracks = None
        self.log = logging.getLogger(self.__class__.__name__)
        self.temp_files = set()
        atexit.register(self.remove_temp_files)

    def progress_cb(self, track, position, length):
        pass

    def track_skip_cb(self, track):
        pass

    def track_start_cb(self, track):
        pass

    def track_end_cb(self, track):
        pass

    def read_cb(self):
        pass

    def remove_temp_files(self):
        for path in self.temp_files:
            try:
                os.unlink(path)
            except (OSError, IOError):
                pass

    def urlopen(self, *args, **kw):
        res = urllib2.urlopen(*args, **kw)
        self.log.debug([res.code, res.msg])
        self.log.debug('headers:\n%s' % ''.join(res.headers.headers))
        return res

    def handshake(self):
        '''Do session handshake. Returns urllib2.Response
        '''
        log = self.log
        passwordmd5 = self.passwordmd5
        url = self.handshake_url % (VERSION, self.username, passwordmd5)
        log.debug('handshake_url: %s', url)
        log.info('Initiating handshake')
        res = self.urlopen(url)
        try:
            vars = dict(self.parse_vars(res.fp))
        except ValueError, e:
            log.error('Bad server response: %s', e, exc_info=True)
        log.debug('vars:\n%s' % pformat(vars))
        try:
            self.session = vars['session']
        except KeyError, e:
            message = 'No data in server response: %s'
            raise HandshakeError(message, *e.args)
        if self.session == 'FAILED':
            raise HandshakeError(vars['msg'])
        return res

    def adjust(self, url):
        '''Adjust radio to given Last.fm URL. Returns urllib2.Response
        '''
        session = self.session
        log = self.log
        if not session:
            raise SessionError('No session. Call handshake() first.')
        log.info('Tunning to "%s"', url)
        res = self.urlopen(self.adjust_url % (session, url))
        try:
            vars = dict(self.parse_vars(res.fp))
        except ValueError, e:
            log.error('Bad server response: %s', e, exc_info=True)
        log.debug('vars:\n%s' % pformat(vars))
        if vars.get('response') != 'OK':
            if vars['error'] == '1':
                raise NoContentAvailable
            if vars['error'] == '4':
                raise InvalidURL(url)
            raise AdjustError('Bad server response. Variables:\n%s',
                    pformat(vars))
        try:
            self.station_name = vars['stationname']
        except KeyError, e:
            log.error('data: %s')
            raise AdjustError('No data in server response: %s', *e.args)
        self.station_name = vars['stationname']
        log.info('Tuned to %s', self.station_name)
        return res

    def xspf(self, discovery=False):
        '''Fetch and parse XSPF playlist saving result in self.tracks.
           Returns urllib2.Response
        '''
        discovery = int(discovery)
        session = self.session
        if not session:
            raise SessionError('No session. Call handshake() first.')
        res = self.urlopen(self.xspf_url % (session, discovery, VERSION))
        self.parse_xspf(res.fp)
        return res

    def parse_vars(self, fp):
        try:
            lines = [ l.strip() for l in fp ] 
        finally:
            fp.close()
        self.log.debug('data:\n%s', '\n'.join(lines))
        return [ line.strip().split('=', 1) for line in lines ]

    def parse_xspf(self, fp):
        h = XSPFHandler()
        try:
            xml.sax.parse(fp, h)
        finally:
            fp.close()
        self.tracks = []
        for track in h.tracks:
            try:
                track = Track(track)
            except ValueError, e:
                log.error('%s', e, exc_info=True)
                continue
            self.tracks.append(track)

        self.log.debug('tracks:\n%s', pformat(self.tracks))
        self.log.info('Tracks:\n%s',
            ''.join([ '%s\n' % t.name for t in self.tracks ]))

    def handle_tracks(self):
        if not os.path.exists(self.outdir):
            os.makedirs(self.outdir)
        for track in self.tracks:
            if self.skip_existing_track(track):
                self.call(self.track_skip_cb, track)
                continue
            try:
                self.handle_track(track)
            except KeyboardInterrupt:
                self.call(self.track_skip_cb, track)
                self.log.info('Interrupted. Skipping track.')
                time.sleep(0.2)
            except SkipTrack:
                self.call(self.track_skip_cb, track)
            except Exception, e:
                self.log.exception('handle_tracks: %s', e)
                self.log.error('Skipping track.')
                self.call(self.track_skip_cb, track)

    def call(self, callback, *args, **kw):
        try:
            callback(*args, **kw)
        except Exception, e:
            msg = 'Unhandled exception in callback: %s: %s'
            self.log.exception(msg, callback, e)

    def handle_track(self, track):
        log = self.log
        self.call(self.track_start_cb, track)
        # Handle audio/mpeg stream
        prefix = '.%s.' % track.make_filename()
        fd, tmp = tempfile.mkstemp(dir=self.outdir, prefix=prefix)
        log.debug('tmp: %s', tmp)
        self.temp_files.add(tmp)
        fp = os.fdopen(fd, 'w+b')
        exceptions = (IOError, OSError, socket.error, httplib.HTTPException)
        log.info(track.name)
        try:
            self.handle_stream(track, fp)
        except urllib2.HTTPError, e:
            log.exception(e)
            log.info('Skipping %s', msg)
            raise SkipTrack
        except exceptions, e:
            log.exception(e)
        else:
            self.finish_track(track, fp, tmp)
            self.call(self.track_end_cb, track)
        finally:
            if os.path.exists(tmp):
                log.debug('Removing %s', tmp)
                try:
                    os.unlink(tmp)
                except (IOError, OSError):
                    pass

    def skip_existing_track(self, track):
        if not self.skip_existing:
            return
        existing = track.find_existing(self.outdir)
        if not existing:
            return
        self.log.info('Skipping existing: %s', existing)
        self.skip_track(track)
        return existing

    def skip_track(self, track):
        '''Try to read a small portion of stream and proceed to next
        track
        '''
        try:
            res = self.urlopen(track['location'])
        except urllib2.HTTPError, e:
            self.log.error('%s', e, exc_info=True)
            return
        data = None
        while data is None:
            try:
                r = self._socket_select(res)
            except (IOError, OSError), e:
                self.log.exception('skip_track: select: %s', e)
                continue
            try:
                data = res.fp.read(SOCKET_READ_SIZE)
            except (socket.error, IOError, OSError), e:
                self.log.exception('skip_track: read: %s', e)

    def finish_track(self, track, fp, tmp):
        self.log.debug('\n')
        self.add_tags(track, tmp)
        fullpath = self.make_track_dirs(track)
        fp.close()
        # shutil.move() may not work reliably with FS that doesn't support
        # mode/ownership attributes (e.g. FAT)
        try:
            shutil.copyfile(tmp, fullpath)
        except (OSError, IOError), e:
            self.log.error('Cannot copy %s to %s', tmp, fullpath, exc_info=True)
            return
        os.unlink(tmp)
        self.log.info('Saved to %s', fullpath)

    def add_tags(self, track, path):
        '''Add ID3 title, album, artist tags to the file if mutagen available
        '''
        if mutagen is None:
            return
        log = self.log
        from mutagen import id3, mp3
        f = mp3.MP3(path)
        if f.tags is None:
            f.add_tags()
        f.tags.add(id3.TIT2(encoding=3, text=track['title']))
        f.tags.add(id3.TALB(encoding=3, text=track['album']))
        f.tags.add(id3.TPE1(encoding=3, text=track['creator']))
        try:
            f.save()
        except mutagen.id3.error, e:
            log.error('Failed to save tags: %s', e, exc_info=True)

    def make_track_dirs(self, track):
        # Make all dirs in path
        outdir = self.outdir
        trackpath = track.getpath(self.strip_windows_incompat,
                                  self.strip_spaces)
        trackdir = os.path.join(outdir, os.path.dirname(trackpath))
        self.log.debug('Track dir: %s', trackdir)
        if not os.path.exists(trackdir):
            os.makedirs(trackdir)
        fullpath = os.path.join(outdir, trackpath)
        return fullpath

    def get_content_length(self, res):
        headers = res.headers.headers
        length = [ int(h.strip().split(': ', 1)[1])
                   for h in headers if h.startswith('Content-Length') ][0]
        return length

    def handle_stream(self, track, fp):
        '''Write `track` audio stream to `fp`
        '''
        log = self.log
        res = self.urlopen(track['location'])
        try:
            length = self.get_content_length(res)
        except ValueError:
            log.error('Failed to get Content-Length')
            return

        count = 0
        while True:
            try:
                r = self._socket_select(res)
            except (IOError, OSError), e:
                log.exception('handle_stream: select: %s', e)
                continue
            if not r:
                log.error('Read timeout reached')
                break
            try:
                data = res.fp.read(SOCKET_READ_SIZE)
                count += len(data)
                fp.write(data)
            except (socket.error, IOError, OSError), e:
                log.exception('handle_stream: read: %s', e)
            else:
                self.call(self.progress_cb, track, count, length)
            if count >= length:
                fp.flush()
                break

    def _socket_select(self, res):
        for i in range(int(SOCKET_TIMEOUT * 10)):
            # XXX A workaround for issue #1327971
            # http://bugs.python.org/issue1327971
            fileno = res.fp._sock.fp.fileno()
            r, w, x = select.select([fileno], [], [fileno], 0.1)
            if r:
                break
            self.call(self.read_cb)
        return r

    def loop(self, urls):
        log = self.log
        self.handshake()
        log.info('Output directory is %s', self.outdir)
        for url in urls:
            while True:
                try:
                    self.adjust(url)
                except InvalidURL, e:
                    log.error('%s', e)
                    break
                except NoContentAvailable:
                    log.info('No content available for "%s"', url)
                    break
                except AdjustError, e:
                    log.error('Failed to tune to "%s": %s', url, e)
                    break
                except (httplib.HTTPException, urllib2.URLError, IOError,
                        socket.error), e:
                    log.error('Failed to tune to "%s": %s', url, e,
                              exc_info=True)
                    break

                delay = BackoffDelay()
                while True:
                    try:
                        self.xspf()
                    except urllib2.HTTPError, e:
                        if e.code == 503:
                            delay.sleep()
                    else:
                        break
                self.handle_tracks()
                # Pause for a while to let user some time to interrupt loop
                time.sleep(0.5)
            time.sleep(0.5)


class Config(object):
    filename = os.path.join(DOTDIR, 'config.cfg')

    class __metaclass__(type):
        bool_vars = ['strip_windows_incompat', 'strip_spaces',
                        'skip_existing', 'save', 'debug']
        login_vars = ['username', 'passwordmd5']
        str_vars = ['outdir', 'station', 'station_type']

        def __new__(mcls, name, bases, namespace):
            for option in mcls.bool_vars:
                get, set, delete = mcls.make_accessors(option, 'options', 'getboolean')
                namespace[option] = property(get, set, delete)
            for option in mcls.login_vars:
                get, set, delete = mcls.make_accessors(option, 'lastfm_user', 'get')
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


class SkipTrack(BaseException):
    pass

if pygtk:
    class RecordStopButton(gtk.Button):
        def __init__(self, *args, **kw):
            super(RecordStopButton, self).__init__(*args, **kw)
            self.set_flags(gtk.CAN_DEFAULT)
            self.record = gtk.Image()
            size = gtk.ICON_SIZE_BUTTON
            self.record.set_from_stock(gtk.STOCK_MEDIA_RECORD, size)
            self.stop = gtk.Image()
            self.stop.set_from_stock(gtk.STOCK_MEDIA_STOP, size)
            self.set_image(self.record)

        def toggle(self):
            if self.is_record:
                self.set_image(self.stop)
            else:
                self.set_image(self.record)

        @property
        def is_record(self):
            return self.get_image() == self.record


class GUI(object):
    class LoopBreak(BaseException):
        pass

    def __init__(self, config, options, urls):
        self.log = log = logging.getLogger(self.__class__.__name__)
        log.debug('config: %s', dict(config))
        log.debug('options: %s', vars(options))
        self.config = config
        self.options = options
        self.urls = urls
        self.radio_client = RadioClient()
        self.break_loop = False
        self.skip_track = False
        self.radio_thread = None

        self.builder = builder = gtk.Builder()
        filename = '%s.glade' % NAME
        # Check if this is pyinstaller --onefile
        moduledir = os.environ.get('_MEIPASS2', os.path.dirname(sys.argv[0]))
        gladepath = os.path.join(moduledir, filename)
        if not os.path.exists(gladepath):
            moduledir = os.path.join(sys.prefix, 'share', NAME)
        gladepath = os.path.join(moduledir, filename)
        builder.add_from_file(gladepath)

        self.window = builder.get_object('mainwindow')
        if not IS_WINDOWS:
            icondir = os.path.join(sys.prefix, 'share', 'pixmaps')
            iconpath = os.path.join(icondir, '%s.png' % NAME)
            if not os.path.exists(iconpath):
                iconpath = os.path.join(moduledir, '%s.png' % NAME)
            #self.icon = gtk.status_icon_new_from_file(iconpath)
            self.window.set_icon_from_file(iconpath)

        self.username = builder.get_object('username')
        self.password = builder.get_object('password')
        self.login = builder.get_object('login')
        self.loginapply = self.builder.get_object('loginapply')

        self.station = builder.get_object('station')
        self.station_type = builder.get_object('stationtype')
        self.station_store = builder.get_object('stationstore')
        cell = gtk.CellRendererText()
        self.station_type.pack_start(cell)
        self.station_type.add_attribute(cell, 'text', 0)

        self.outdir = builder.get_object('outdir')
        self.outdir.set_action(gtk.FILE_CHOOSER_ACTION_SELECT_FOLDER)

        strip_windows_incompat = builder.get_object('strip_windows_incompat')
        self.strip_windows_incompat = strip_windows_incompat
        self.strip_spaces = builder.get_object('strip_spaces')
        self.skip_existing = builder.get_object('skip_existing')

        self.loginsave = builder.get_object('loginsave')
        self.statusbar = builder.get_object('statusbar')
        self.context_id = self.statusbar.get_context_id('Station')
        self.progress = builder.get_object('progress')
        self.init_progress()

        self.next = builder.get_object('next')
        self.record_stop = RecordStopButton()
        self.record_stop.show()
        buttons = self.builder.get_object('buttons')
        buttons.pack_start(self.record_stop, expand=False, fill=False,
                           padding=4)
        buttons.reorder_child(self.next, 1)

        self.init_view()
        self.connect_signals()
        self.window.show()

    def connect_signals(self):
        self.outdir.connect('current_folder_changed',
                            self.on_outdir_current_folder_changed)
        self.strip_windows_incompat.connect('toggled', self.on_option_toggled,
                                            dict(name='strip_windows_incompat'))
        self.skip_existing.connect('toggled', self.on_option_toggled,
                                   dict(name='skip_existing'))
        self.strip_spaces.connect('toggled', self.on_option_toggled,
                                  dict(name='strip_spaces'))
        self.loginsave.connect('toggled', self.on_option_toggled,
                               dict(name='save'))

        self.username.connect('changed', self.on_username_changed)
        self.username.connect('key-press-event',
                              self.on_username_key_press_event)
        self.password.connect('key-press-event',
                              self.on_password_key_press_event)
        self.station.connect('changed', self.on_station_changed)
        self.station_type.connect('changed', self.on_station_type_chanded)
        self.station_type.connect('grab_focus', self.on_station_type_chanded)

        self.loginapply.connect('clicked', self.on_loginapply_clicked)

        self.record_stop.connect('clicked', self.on_record_stop_clicked)

        self.next.connect('clicked', self.on_next_clicked)

        self.window.connect('destroy', self.on_window_destroy)

    def init_radio_thread(self):
        self.radio_thread = threading.Thread(name='radio', target=self.loop)
        self.radio_thread.daemon = True

    def init_progress(self):
        self.progress.set_text('Idle')
        self.progress.set_fraction(0)

    def url_status_message(self):
        self.update_status('Station URL: %s' % self.station_url)

    def update_status(self, message):
        message = str(message)
        self.log.debug('Status: %s', message)
        self.statusbar.push(self.context_id, message)

    @property
    def station_url(self):
        row = self.station_store[self.station_type.get_active()]
        self.log.debug('station row: %s', list(row))
        template = row[1]
        self.config.station_type = row[2]
        url = template % dict(username=self.options.username,
                              station=self.station.get_text())
        if self.options.quote:
            url = quote_url(url)
        return url

    def grab_default(self):
        if not self.options.username or not self.options.passwordmd5:
            self.loginapply.grab_default()
            self.log.debug('grab_default: loginapply')
        else:
            self.record_stop.grab_default()
            self.log.debug('grab_default: record_stop')
        row = self.station_store[self.station_type.get_active()]
        station_type = row[2]
        arg_required = row[3]
        if not arg_required: 
            self.station.set_sensitive(False)
            self.station.set_text('')
            self.record_stop.grab_focus()
            self.log.debug('grab_default: focus: record_stop')
        else:
            self.station.set_sensitive(True)
            self.station.grab_focus()
            self.log.debug('grab_default: focus: station')
            if station_type == 'custom':
                self.station.select_region(-1, -1)


    def init_view(self):
        options = self.options
        self.outdir.set_current_folder(options.outdir)
        self.strip_windows_incompat.set_active(options.strip_windows_incompat)
        self.strip_spaces.set_active(options.strip_spaces)
        self.skip_existing.set_active(options.skip_existing)
        self.loginsave.set_active(options.save)

        self.station.set_text(self.config.station or '')
        station_type = self.config.station_type 
        if station_type:
            iter = [ row.iter for row in self.station_store
                     if row[2] == station_type ][0]
            self.station_type.set_active_iter(iter)
        else:
            self.station_type.set_active(0)

        self.url_status_message()

        username = self.options.username
        passwordmd5 = self.options.passwordmd5
        login = self.login

        if not username:
            login.set_expanded(True)
            self.username.grab_focus()
            return
        self.username.set_text(username)

        if not passwordmd5:
            login.set_expanded(True)
            self.password.grab_focus()
            return
        self.password.set_text('*' * 10)
        self.grab_default()

    def update_password(self):
        if self.options.save:
            self.config.passwordmd5 = self.options.passwordmd5 
        else:
            self.config.clear_password()

    def update_config(self):
        self.log.debug('Options: %s', vars(self.options))
        for field in ['skip_existing', 'strip_windows_incompat',
                      'strip_spaces', 'save', 'outdir', 'username']:
            setattr(self.config, field, getattr(self.options, field))
        self.log.debug('Updated config: %s', dict(self.config))

    def write_config(self):
        try:
            self.config.write()
        except (IOError, OSError), e:
            self.log.exception('Error saving config file: %s', e)

    def loop(self):
        try:
            self.handle_radio()
        except self.LoopBreak:
            idle_add(self.update_status, 'Stopped')
        except HandshakeError:
            idle_add(self.init_record)
            idle_add(self.record_stop.toggle)
            idle_add(self.login_error)
        except Exception, e:
            self.log.exception('loop: %s', e)
        else:
            idle_add(self.init_record)

    def handle_radio(self):
        log = self.log
        url = self.station_url
        radio = self.radio_client

        idle_add(self.update_status, 'Logging in ...')
        radio.handshake()
        idle_add(self.update_status, '')
        if self.break_loop:
            raise self.LoopBreak

        while True:
            if self.break_loop:
                raise self.LoopBreak
            idle_add(self.update_status, 'Tuning to %s ...' % url)
            try:
                radio.adjust(url)
            except InvalidURL, e:
                idle_add(self.update_status, e)
                idle_add(self.station.grab_focus)
                return
            except NoContentAvailable:
                idle_add(self.update_status,
                         'No content available for %s' % url)
                idle_add(self.station_type.grab_focus)
                return
            except AdjustError, e:
                idle_add(self.update_status,
                         'Failed to tune to %s: %s' % (url, e))
                idle_add(self.station_type.grab_focus)
                return
            except (httplib.HTTPException, urllib2.URLError, IOError,
                    socket.error), e:
                msg = 'Failed to tune to %s: %s' % (url, e)
                log.exception(msg)
                idle_add(self.update_status, msg)
                return
            else:
                idle_add(self.update_status, '')
            finally:
                idle_add(self.grab_default)

            delay = BackoffDelay()
            while True:
                if self.break_loop:
                    raise self.LoopBreak

                idle_add(self.update_status, 'Requesting tracks ...')
                try:
                    radio.xspf()
                except urllib2.HTTPError, e:
                    if e.code == 503:
                        delay.sleep()
                else:
                    idle_add(self.update_status, '')
                    break

            radio.handle_tracks()

    def login_error(self):
        self.update_status('Login error.'
                           ' Please check your username and password.')
        self.options.passwordmd5 = None
        self.grab_default()
        self.login.set_expanded(True)
        self.password.grab_focus()

    def check_falgs(self):
        if self.break_loop:
            self.break_loop = False
            raise self.LoopBreak
        if self.skip_track:
            self.skip_track = False
            raise SkipTrack

    def progress_cb(self, track, position, length):
        self.check_falgs()
        fraction = float(position) / float(length)
        percent = fraction * 100
        msg = '%s: %0.1f%%' % (track.name, percent)
        idle_add(self.progress.set_fraction, fraction)
        idle_add(self.progress.set_text, msg)

    def read_cb(self):
        self.check_falgs()

    def track_start_cb(self, track):
        self.check_falgs()
        idle_add(self.progress.set_text, track.name)
        idle_add(self.update_status, track.name)

    def track_end_cb(self, track):
        self.check_falgs()
        idle_add(self.init_progress)
        idle_add(self.update_status, '')

    def track_skip_cb(self, track):
        self.check_falgs()
        idle_add(self.update_status, 'Skipped %s' % track.name)

    def on_window_destroy(self, widget, data=None):
        self.break_loop = True
        if self.radio_thread is not None and self.radio_thread.isAlive():
            self.radio_thread.join()
        self.update_password()
        self.update_config()
        self.write_config()
        gtk.main_quit()

    def on_record_stop_clicked(self, widget, data=None):
        record_stop = widget
        if record_stop.is_record:
            for name in ['username', 'passwordmd5', 'outdir', 'skip_existing',
                         'strip_windows_incompat', 'strip_spaces']:
                value = getattr(self.options, name)
                setattr(self.radio_client, name, value)
            self.radio_client.progress_cb = self.progress_cb
            self.radio_client.track_start_cb = self.track_start_cb
            self.radio_client.track_end_cb = self.track_end_cb
            self.radio_client.track_skip_cb = self.track_skip_cb
            self.radio_client.read_cb = self.read_cb
            self.break_loop = False
            self.skip_track = False
            self.init_radio_thread()
            self.radio_thread.start()
        else:
            self.init_record()
            self.break_loop = True
            if self.radio_thread is not None:
                self.radio_thread.join()
            self.radio_thread = None
            self.url_status_message()

        record_stop.toggle()

    def init_record(self):
        self.record_stop.show()
        self.record_stop.grab_focus()
        self.init_progress()

    def on_next_clicked(self, widget, data=None):
        if self.radio_thread is not None and self.radio_thread.isAlive():
            self.skip_track = True
            self.init_progress()

    def on_username_changed(self, widget, data=None):
        self.options.username = widget.get_text()

    def on_username_key_press_event(self, widget, event, data=None):
        # TODO: Doesn't work on Windows
        if event.type != gtk.gdk.KEY_PRESS:
            return
        if event.keyval == gtk.keysyms.Return:
            self.password.grab_focus()

    def on_password_key_press_event(self, widget, event, data=None):
        self.options.passwordmd5 = md5(widget.get_text()).hexdigest()
        self.loginapply.grab_default()

    def on_outdir_current_folder_changed(self, widget, data=None):
        self.options.outdir = widget.get_filename()

    def on_station_changed(self, widget, data=None):
        self.config.station = widget.get_text()
        self.url_status_message()

    def on_station_type_chanded(self, widget, data=None):
        row = self.station_store[widget.get_active()]
        station_type = row[2]
        arg_required = row[3]
        self.config.station_type = station_type 
        if station_type == 'custom':
            self.station.set_text('lastfm://')
            self.station.select_region(-1, -1)
        elif not self.station.get_text:
            self.station.set_text('')

        self.url_status_message()
        self.grab_default()

    def on_option_toggled(self, widget, data):
        value = widget.get_active()
        name = data['name']
        setattr(self.options, name, value)
        if name == 'save':
            self.update_password()

    def on_loginapply_clicked(self, *args, **kw):
        self.update_password()
        self.write_config()
        self.login.set_expanded(False)
        self.station_type.grab_focus()
        self.grab_default()


def setup_urllib2():
    '''Set cookie processor and default HTTP headers.
    '''
    opener = urllib2.build_opener(urllib2.HTTPCookieProcessor())
    opener.addheaders = [('User-Agent', USER_AGENT)]
    urllib2.install_opener(opener)


def quote_url(url):
    q = urllib2.quote
    i = len('lastfm:')
    return url[:i] + q(url[i:])


def parse_args(config, defaults):
    parser = OptionParser(usage=__doc__.rstrip())

    defaults.update((k,v) for k, v in config if v is not None)
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
        q = urllib2.quote
        i = len('lastfm:')
        args = [ quote_url(arg) for arg in args ]

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
    format= '%(asctime)s %(levelname)8s %(name)s: %(message)s'
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
        passwordmd5 = md5(getpassword(username)).hexdigest()

    assert username, "No username specified"
    assert passwordmd5, "No password specified"

    return username, passwordmd5


def gui_main(config=None, options=None, urls=None):
    if config is None:
        DEFAULTS['gui'] = True
        config, options, urls = setup(DEFAULTS.copy())
    gui = GUI(config, options, urls)
    gtk.main()


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
