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

import atexit
import httplib
import logging
import os
import select
import shutil
import socket
import tempfile
import time
import urllib2
import xml.sax

from pprint import pformat

try:
    import mutagen
except ImportError:
    mutagen = None
from lastrecorder.exceptions import SkipTrack
from lastrecorder import util

SOCKET_READ_SIZE = 512
SOCKET_TIMEOUT = 30
# Pretend to be Last.fm player
VERSION = '1.5.1.31879'
USER_AGENT = 'User-Agent: Last.fm Client %s (X11)' % VERSION

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
        super(InvalidURL, self).__init__()
        self.url = url

    def __str__(self):
        return self.message % self.url


class NoContentAvailable(Error):
    pass


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

    def find_existing(self, directory):
        '''Find existing files for this track checking all possible naming
        schemes. Returns first matched path
        '''
        # Get list of all possible binary combinations of given length
        l = 2
        masks = [ 1 << i - 1 for i in range(l, 0, -1) ]
        binary_combinations = [ map(lambda x: bool(x & i), masks)
                                for i in range(1 << l) ]
        for args in binary_combinations:
            path = os.path.join(directory, self.getpath(*args))
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
                self.log.error('%s', e, exc_info=True)
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
            log.info('Skipping %s', track.name)
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

                delay = util.BackoffDelay()
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


def setup_urllib2():
    '''Set cookie processor and default HTTP headers.
    '''
    opener = urllib2.build_opener(urllib2.HTTPCookieProcessor())
    opener.addheaders = [('User-Agent', USER_AGENT)]
    urllib2.install_opener(opener)
