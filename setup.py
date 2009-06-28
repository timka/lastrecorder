import os
from setuptools import setup

name = 'LastRecorder'
module = name.lower()

setup(
    name = name, 
    version = "0.5",
    py_modules = [module],
    entry_points = dict(
        console_scripts = [
            '%s-cli = %s:cli_main' % (module, module),
        ],
        gui_scripts = [
            '%s = %s:gui_main' % (module, module),
        ],
    ),
    data_files = [
        (os.path.join('share', 'applications'), ['%s.desktop' % module]),
        (os.path.join('share', 'pixmaps'), ['%s.png' % module]),
        (os.path.join('share', module), ['%s.glade' % module]),
    ],
)
