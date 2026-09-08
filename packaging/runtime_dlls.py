"""Run before application imports/threads in the packaged onefile executable."""
from agent_manager.platform.dlls import install

install()
