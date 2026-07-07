"""Identity and authorization layer.

This module contains:
- Principal model (humans, AI agents, services)
- AuthProvider port (authentication abstraction)
- Capability management
- Delegation support
"""

from ontolith.identity.principal import Principal, min_capability

__all__ = ["Principal", "min_capability"]
