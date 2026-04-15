"""Abstract base class for storage backends."""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class StorageBackend(ABC):
    """Abstract storage backend. All methods are async."""

    @abstractmethod
    async def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        """Store a value with optional TTL."""
        ...

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        """Retrieve a value by key. Returns None if not found."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete a key."""
        ...

    @abstractmethod
    async def keys(self, pattern: str) -> List[str]:
        """Return all keys matching a glob pattern."""
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if key exists."""
        ...

    @abstractmethod
    async def hset(self, name: str, key: str, value: Any) -> None:
        """Set a hash field."""
        ...

    @abstractmethod
    async def hget(self, name: str, key: str) -> Optional[Any]:
        """Get a hash field."""
        ...

    @abstractmethod
    async def hgetall(self, name: str) -> Dict[str, Any]:
        """Get all hash fields."""
        ...

    @abstractmethod
    async def hdel(self, name: str, key: str) -> None:
        """Delete a hash field."""
        ...

    @abstractmethod
    async def publish(self, channel: str, message: str) -> None:
        """Publish a message to a channel (pub/sub)."""
        ...

    @abstractmethod
    async def subscribe(self, channel: str) -> Any:
        """Subscribe to a channel. Returns async generator of messages."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection."""
        ...

    @abstractmethod
    async def ping(self) -> bool:
        """Health check. Returns True if healthy."""
        ...

    async def initialize(self) -> None:
        """Optional initialization logic."""
        await self.connect()
