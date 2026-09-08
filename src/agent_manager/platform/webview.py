"""Small dependency probe; never loads .NET or opens a window."""
import os
import re

EDGE_CLIENTS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",
)


def available(registry=None) -> bool:
    if os.name != "nt" and registry is None:
        return False
    if registry is None:
        import winreg as registry
    try:
        with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
            release = registry.QueryValueEx(key, "Release")[0]
        if int(release) < 394802:
            return False
    except (OSError, ValueError, TypeError):
        return False
    for hive in (registry.HKEY_CURRENT_USER, registry.HKEY_LOCAL_MACHINE):
        for prefix in (r"SOFTWARE\Microsoft\EdgeUpdate\Clients", r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"):
            for client in EDGE_CLIENTS:
                try:
                    with registry.OpenKey(hive, prefix + "\\" + client) as key:
                        version = str(registry.QueryValueEx(key, "pv")[0])
                    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
                    if match and tuple(map(int, match.groups())) >= (86, 0, 622):
                        return True
                except OSError:
                    continue
    return False
