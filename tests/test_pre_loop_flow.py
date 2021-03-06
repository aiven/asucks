from asucks.base_server import AddressType, Command, ConnectionInfo, HandshakeError, Method, ProxyConnection, ServerConfig
from asyncio.streams import StreamReader, StreamWriter
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Optional

import aiohttp
import asyncio
import ipaddress
import json
import pytest

pytestmark = pytest.mark.asyncio


class TestResponse:
    def __init__(self, fails: bool, authenticates: bool, valid_json: bool):
        self.authenticates = authenticates
        self.ok = fails
        self.valid_json = valid_json
        self.status = 200 if fails else 500

    async def json(self):
        if not self.valid_json:
            raise json.JSONDecodeError("foo", "foo\nfoo", 0)
        if self.authenticates:
            return {"decision": "authenticated"}
        return {"decision": "not_authenticated"}


class TestHttpCli:
    # pylint: disable=super-init-not-called
    def __init__(self, fails: bool, authenticates: bool, valid_json: bool):
        self.authenticates = authenticates
        self.fails = fails
        self.valid_json = valid_json

    @asynccontextmanager
    # pylint: disable=unused-argument
    async def post(self, *args, **kwargs):
        try:
            yield TestResponse(self.fails, self.authenticates, self.valid_json)
        finally:
            pass


class TestConnection(ProxyConnection):
    # pylint: disable=super-init-not-called
    def __init__(
        self,
        source_buff: BytesIO,
        reader: Optional[StreamReader] = None,
        writer: Optional[StreamWriter] = None,
        config: Optional[ServerConfig] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        http_client: Optional[aiohttp.ClientSession] = None
    ):
        config = config or ServerConfig(
            host="0.0.0.0",
            port=1080,
            username="foo",
            password="foopass",
            validator=None,
            ca_file=None,
        )
        super().__init__(writer=writer, reader=reader, config=config, http_client=http_client, loop=loop)
        self.source_buff = source_buff
        self.response_data: Optional[bytes] = None
        self.dst_address = "destination-address"
        self.closed = False

    @property
    def src_address(self):
        return "source-address"

    async def source_read(self, count: int) -> bytes:
        return self.source_buff.read(count)

    async def source_write(self, data) -> None:
        self.response_data = data
        return None

    async def destination_read(self, count: int) -> bytes:
        return b""

    async def destination_write(self, data) -> None:
        return None

    async def close_all(self):
        self.closed = True


@pytest.mark.parametrize(
    "payload", [
        (b"\x05\x01\x01", {Method.gssapi}),
        (b"\x05\x01\x00", {Method.no_auth}),
        (b"\x05\x01\x02", {Method.user_pass}),
        (b"\x05\x02\x00\x01", {Method.gssapi, Method.no_auth}),
        (b"\x05\x02\x00\x02", {Method.no_auth, Method.user_pass}),
        (b"\x05\x03\x00\x01\x02", {Method.user_pass, Method.no_auth, Method.gssapi}),
    ]
)
async def test_method_parse(payload):
    payload, expected = payload
    conn = TestConnection(source_buff=BytesIO(payload))
    methods = await conn.get_auth_methods()
    assert methods == expected


async def test_process_fails():
    conn = TestConnection(source_buff=BytesIO(b"\x05\x02\x00"))
    await conn.process_request()
    assert conn.closed


@pytest.mark.parametrize("payload", [
    b"\x04",
    b"\x05\x01",
    b"\x05\x02\x00",
])
async def test_method_fail_parse(payload):
    conn = TestConnection(source_buff=BytesIO(payload))
    with pytest.raises(HandshakeError):
        methods = await conn.get_auth_methods()
        assert not methods


@pytest.mark.parametrize("payload", [
    (b"", {Method.no_auth}),
    (b"\x01\x03foo\x07foopass", {Method.user_pass}),
])
async def test_auth_passes(payload):
    payload, methods = payload
    conn = TestConnection(source_buff=BytesIO(payload))
    # Drop the user name and pass
    if not payload:
        conn.config.password = None
        conn.config.username = None
    await conn.authenticate(methods)


@pytest.mark.parametrize(
    "payload", [
        (b"", {Method.user_pass}),
        (b"\x01\x03foo\x07foopas", {Method.user_pass}),
        (b"\x01\x03foo\x07foopass", {Method.gssapi}),
        (b"\x02\x03foo\x07foopas", {Method.user_pass}),
        (b"\x01\x03foo\x06foopass", {Method.user_pass}),
    ]
)
async def test_auth_fails(payload):
    payload, methods = payload
    conn = TestConnection(source_buff=BytesIO(payload))
    with pytest.raises(HandshakeError):
        await conn.authenticate(methods)


@pytest.mark.parametrize(["request_ok", "auth_ok", "valid_json"], [
    [True, True, True],
    [False, True, True],
    [True, False, True],
    [True, False, False],
    [False, False, True],
])
async def test_external_auth(request_ok, auth_ok, valid_json):
    config = ServerConfig(
        host="0.0.0.0",
        port=1080,
        username="foo",
        password="foopass",
        validator="some",
        ca_file="thing",
    )
    conn = TestConnection(source_buff=BytesIO(b""), http_client=TestHttpCli(request_ok, auth_ok, valid_json), config=config)
    resp = await conn.auth_ok("foo", "bar")
    assert resp is (request_ok and auth_ok and valid_json)


@pytest.mark.parametrize(
    "data", [
        (
            b"\x05\x01\x00\x01\x01\x01\x01\x01\x00\x00",
            ConnectionInfo(
                Command.connect, AddressType.ipv4, ipaddress.ip_address("1.1.1.1"), 0, b"\x01\x01\x01\x01\x00\x00"
            )
        ),
        (
            b"\x05\x01\x00\x04" + b"\x01" * 16 + b"\x00\x00",
            ConnectionInfo(
                Command.connect, AddressType.ipv6, ipaddress.ip_address("101:101:101:101:101:101:101:101"), 0,
                b"\x01" * 16 + b"\x00\x00"
            )
        ),
        (
            b"\x05\x01\x00\x03\x08asdf.com\x00\x00",
            ConnectionInfo(Command.connect, AddressType.fqdn, "asdf.com", 0, b"\x08asdf.com\x00\x00")
        ),
    ]
)
async def test_address_info_passes(data):
    payload, resp = data
    conn = TestConnection(source_buff=BytesIO(payload))
    conn_info = await conn.get_destination_info()
    if conn_info.address_type is AddressType.fqdn:
        # drop it for now, as the ips can be unstable
        conn_info.address = None
        resp.address = None
    assert conn_info == resp


@pytest.mark.parametrize(
    "payload", [
        b"\x04\x01\x00\x01\x01\x01\x01\x01\x00\x00",
        b"\x05\x01\x01\x04\x01\x00\x00",
        b"\x05\x02\x00\x03\x08\x00\x00",
        b"\x05\x01\x00\x01\x01\x01\x01\x00",
        b"\x05\x01\x00\x04\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01",
    ]
)
async def test_address_info_failure(payload):
    conn = TestConnection(source_buff=BytesIO(payload))
    with pytest.raises(HandshakeError):
        resp = await conn.get_destination_info()
        assert resp is None
