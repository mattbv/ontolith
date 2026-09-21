"""Identity and authorization layer.

This module contains:
- Principal model (humans, AI agents, services)
- AuthProvider port (authentication abstraction)
- Capability management
- Delegation support

`TokenAuthProvider`/`hash_token` (the concrete `AuthProvider` implementation,
ADR-0014) are deliberately NOT re-exported here — `import
ontolith.identity.token_auth` from this file would be circular
(`store.base` imports `Principal` from `ontolith.identity`, and
`token_auth.py` imports `StorageBackend` from `store.base`). Import them
directly: `from ontolith.identity.token_auth import TokenAuthProvider,
hash_token` — see that module's own docstring for the same explanation.
"""

from ontolith.identity.admin_event import AdminAction, AdminEvent
from ontolith.identity.credential import PrincipalCredential
from ontolith.identity.ports import AuthProvider
from ontolith.identity.principal import Principal, min_capability

__all__ = [
    "AdminAction",
    "AdminEvent",
    "AuthProvider",
    "Principal",
    "PrincipalCredential",
    "min_capability",
]
