from setuptools import setup

name = "LastRecorder"
module = name.lower()

setup(
    name = name, 
    version = "0.5",
    py_modules = [module],
    entry_points = dict(
        console_scripts = [
            '%s = %s:main' % (module, module),
        ],
        gui_scripts = [
            '%s = %s.gui_main' % (module, module),
        ],
    )
)
