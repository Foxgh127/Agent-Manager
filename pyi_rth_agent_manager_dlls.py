"""Run before application imports/threads in the packaged onefile executable."""
from frozen_dll_isolation import install

install()
