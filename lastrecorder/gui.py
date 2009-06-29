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

import os
import sys
import logging
import threading
import socket
import urllib2
import httplib

import pygtk
pygtk.require("2.0")
import gtk

from gobject import idle_add

gtk.gdk.threads_init()

import lastrecorder

from lastrecorder import NAME, IS_WINDOWS
from lastrecorder.exceptions import SkipTrack
from lastrecorder.radio import (RadioClient, HandshakeError, InvalidURL,
                                NoContentAvailable, AdjustError)
from lastrecorder import util
from lastrecorder import release

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
        self.window.set_title('Last Recorder %s' % release.version)
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
            url = util.quote_url(url)
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
        if self.options.save and self.options.passwordmd5:
            self.config.passwordmd5 = self.options.passwordmd5
        else:
            self.config.clear_password()

    def update_config(self):
        self.log.debug('Options: %s', vars(self.options))
        for field in ['skip_existing', 'strip_windows_incompat',
                      'strip_spaces', 'save', 'outdir', 'username']:
            value = getattr(self.options, field)
            if value is None:
                try:
                    del self.config.field
                except AttributeError:
                    pass
            else:
                setattr(self.config, field, value)
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

            delay = util.BackoffDelay()
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
        self.options.passwordmd5 = util.md5(widget.get_text()).hexdigest()
        self.loginapply.grab_default()

    def on_outdir_current_folder_changed(self, widget, data=None):
        self.options.outdir = widget.get_filename()

    def on_station_changed(self, widget, data=None):
        self.config.station = widget.get_text()
        self.url_status_message()

    def on_station_type_chanded(self, widget, data=None):
        row = self.station_store[widget.get_active()]
        station_type = row[2]
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

    def on_loginapply_clicked(self, widget, data=None):
        self.update_password()
        self.write_config()
        self.login.set_expanded(False)
        self.station_type.grab_focus()
        self.grab_default()


def gui_main(config=None, options=None, urls=None):
    if config is None:
        from lastrecorder.main import setup
        lastrecorder.DEFAULTS['gui'] = True
        config, options, urls = setup(lastrecorder.DEFAULTS.copy())
    gui = GUI(config, options, urls)
    gtk.main()
