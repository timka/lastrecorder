*Last Recorder is not actively maintained currently. If you'd like to help with it please contact the author*.

# lastrecorder, a Last.fm stream recorder

Last Recorder is a small program that can save Last.fm streams as mp3 files.

## Features

* automatic ID3 tags (optional, requires mutagen)
* easy graphical interface (optional, requires pygtk)
* simple command line interface
* selectable standard stations (Tag, Artist, Loved, etc.)
* custom station URL's
* skipping already recorded tracks automatically (optional)
* `<artist>/<album>/<title>.mp3` naming scheme
* stripping Windows-incompatible characters and whitespaces from file names (optional)
* quoting URL's automatically (`'lastfm://globaltags/russian rock' -> 'lastfm://globaltags/russian%20rock'`)
* persistent settings (last used station, options, login credentials)
