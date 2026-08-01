"""Machine-readable skip decorators for the substrate suite.

M2 / gates-green-t-suite-collects. A test that cannot run on this host must say
so in a form automation can budget against, not fail and be triaged by hand
every time. Every reason string here is one of exactly two shapes:

    PLATFORM:<sys.platform>   this OS cannot run it, and no install fixes that
    REQUIRES:<thing>          a dependency or privilege is absent

`EXPECTED_SKIPS.txt` records how many of each are allowed per platform, so a
NEW unconditional skip is a check failure rather than a quiet reduction in
coverage. That is the whole point: skipping is allowed, skipping silently is
not.

Not a test module -- the discovery pattern is `test_*.py`, so this is imported,
never collected.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import platform_compat  # noqa: E402


def _installed(module: str) -> bool:
    """Whether `module` can be imported, without importing it.

    `find_spec` rather than a try/import: several of these packages pull in
    onnxruntime and a model load, which is far too expensive to pay merely to
    decide whether to skip.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


#: The retrieval and embedding stack, installed into the provisioned venv from
#: requirements-artifact-ingestion.lock.txt. Absent from a bare interpreter on
#: EVERY platform -- this is the class that accounts for ~30 errors on Linux and
#: 29 on Windows, so it is a venv gap, not a portability gap.
HAS_QDRANT_STACK = _installed("qdrant_client") and _installed("fastembed")

#: The resident service listens on a Unix-domain socket. Windows has none.
HAS_AF_UNIX = hasattr(socket, "AF_UNIX")

#: Creating a symlink needs SeCreateSymbolicLinkPrivilege on Windows, which is a
#: property of the ACCOUNT (Developer Mode), not of the OS version -- so it is
#: probed, not assumed. A junction is not a symlink and cannot stand in where
#: symlink-ness is what is under test.
HAS_SYMLINKS = platform_compat.supports_symlinks()


requires_qdrant_stack = unittest.skipUnless(
    HAS_QDRANT_STACK, "REQUIRES:qdrant-client+fastembed (provisioned venv)"
)

requires_af_unix = unittest.skipUnless(
    HAS_AF_UNIX, f"PLATFORM:{sys.platform} (no AF_UNIX; the service runs under WSL2)"
)

requires_symlinks = unittest.skipUnless(
    HAS_SYMLINKS, "REQUIRES:symlink-privilege"
)

#: Tests that assert POSIX permission bits directly (`S_IMODE(...) == 0o600`).
#: Windows `os.chmod` only toggles the read-only attribute, so a file created
#: 0o600 reports 0o666 and a directory 0o700 reports 0o777 -- the assertion is
#: checking something the OS does not implement, not a defect in the code.
#: Production paths take the same branch via
#: `platform_compat.supports_posix_privacy()`; see artifact_security.
requires_posix_modes = unittest.skipUnless(
    platform_compat.supports_posix_privacy(),
    f"PLATFORM:{sys.platform} (POSIX mode bits are not an access control here)",
)


__all__ = [
    "HAS_AF_UNIX",
    "HAS_QDRANT_STACK",
    "HAS_SYMLINKS",
    "requires_af_unix",
    "requires_posix_modes",
    "requires_qdrant_stack",
    "requires_symlinks",
]
