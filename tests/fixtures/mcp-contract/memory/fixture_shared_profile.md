---
name: fixture_shared_profile
description: Fixture personal-memory entry for the M2 shared-profile negative test
metadata:
  type: reference
---

WORKSPACE-SHARED-PROFILE-NEGATIVE-MARKER-7f3a1c9e — this content lives ONLY in the
personal-memory tier. Under SERVER_PROFILE=shared it must be absent from every
tool (search_platform_knowledge, search_memory, list_memory, get_memory); under
the default personal profile it must still be served.
