import os
from setuptools import setup, find_packages

name = 'LastRecorder'
module = name.lower()

setup(
    name = name, 
    version = "0.5",
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
