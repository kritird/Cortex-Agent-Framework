"""Redis storage backend with pub/sub and TTL support."""
import json
from typing import Any, AsyncGenerator, Dict, List, Optional

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

from cortex.storage.base import StorageBackend


class RedisBackend(StorageBackend):
    """
    Redis-backed storage with pub/sub for cross-instance signalling.
    Requires redis>=5.0 with asyncio support.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 1,
        password: Optional[str] = None,
        username: Optional[str] = None,
        key_prefix: str = "cortex",
        pool_max_connections: int = 20,
        tls_enabled: bool = False,
        tls_verify_peer: bool = True,
        tls_cert_file: str = "",
        tls_key_file: str = "",
        tls_ca_cert_file: str = "",
    ):
        if not REDIS_AVAILABLE:
            raise ImportError("redis package required for RedisBackend. Install with: pip install redis>=5.0")
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._username = username
        self._key_prefix = key_prefix
        self._pool_max_connections = pool_max_connections
        self._tls_enabled = tls_enabled
        self._tls_verify_peer = tls_verify_peer
        self._tls_cert_file = tls_cert_file
        self._tls_key_file = tls_key_file
        self._tls_ca_cert_file = tls_ca_cert_file
        self._client: Optional[aioredis.Redis] = None

    def _prefixed(self, key: str) -> str:
        return f"{self._key_prefix}:{key}"

    async def connect(self) -> None:
        ssl_context = None
        if self._tls_enabled:
            import ssl
            ssl_context = ssl.create_default_context()
            if not self._tls_verify_peer:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            if self._tls_ca_cert_file:
                ssl_context.load_verify_locations(self._tls_ca_cert_file)
            if self._tls_cert_file and self._tls_key_file:
                ssl_context.load_cert_chain(self._tls_cert_file, self._tls_key_file)

        self._client = aioredis.Redis(
            host=self._host,
            port=self._port,
            db=self._db,
            password=self._password,
            username=self._username,
            max_connections=self._pool_max_connections,
            ssl=ssl_context,
            decode_responses=True,
        )

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        try:
            return await self._client.ping()
        except Exception:
            return False

    async def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        serialized = json.dumps(value)
        pkey = self._prefixed(key)
        if ttl_seconds is not None:
            await self._client.setex(pkey, ttl_seconds, serialized)
        else:
            await self._client.set(pkey, serialized)

    async def get(self, key: str) -> Optional[Any]:
        val = await self._client.get(self._prefixed(key))
        if val is None:
            return None
        return json.loads(val)

    async def delete(self, key: str) -> None:
        await self._client.delete(self._prefixed(key))

    async def keys(self, pattern: str) -> List[str]:
        prefix = self._key_prefix + ":"
        full_pattern = self._prefixed(pattern)
        raw_keys = await self._client.keys(full_pattern)
        return [k[len(prefix):] for k in raw_keys]

    async def exists(self, key: str) -> bool:
        return bool(await self._client.exists(self._prefixed(key)))

    async def hset(self, name: str, key: str, value: Any) -> None:
        await self._client.hset(self._prefixed(name), key, json.dumps(value))

    async def hget(self, name: str, key: str) -> Optional[Any]:
        val = await self._client.hget(self._prefixed(name), key)
        if val is None:
            return None
        return json.loads(val)

    async def hgetall(self, name: str) -> Dict[str, Any]:
        raw = await self._client.hgetall(self._prefixed(name))
        return {k: json.loads(v) for k, v in raw.items()}

    async def hdel(self, name: str, key: str) -> None:
        await self._client.hdel(self._prefixed(name), key)

    async def publish(self, channel: str, message: str) -> None:
        await self._client.publish(self._prefixed(channel), message)

    async def subscribe(self, channel: str) -> AsyncGenerator[str, None]:
        pubsub = self._client.pubsub()
        await pubsub.subscribe(self._prefixed(channel))
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    yield message["data"]
        finally:
            await pubsub.unsubscribe(self._prefixed(channel))
            await pubsub.aclose()
