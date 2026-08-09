"""sockpost - a local message bus for terminal agents.

A single daemon per host owns a Unix domain socket and a durable SQLite queue.
Agents connect with a channel id, receive messages as they arrive, and
acknowledge them. Anything not acknowledged survives restarts and is delivered
again when the channel comes back.
"""

__version__ = "0.2.0"
__all__ = ["__version__"]
