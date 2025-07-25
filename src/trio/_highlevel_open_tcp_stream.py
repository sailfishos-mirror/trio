from __future__ import annotations

import sys
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any

import trio
from trio.socket import SOCK_STREAM, SocketType, getaddrinfo, socket

if TYPE_CHECKING:
    from collections.abc import Generator, MutableSequence
    from socket import AddressFamily, SocketKind

    from trio._socket import AddressFormat

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup


# Implementation of RFC 6555 "Happy eyeballs"
# https://tools.ietf.org/html/rfc6555
#
# Basically, the problem here is that if we want to connect to some host, and
# DNS returns multiple IP addresses, then we don't know which of them will
# actually work -- it can happen that some of them are reachable, and some of
# them are not. One particularly common situation where this happens is on a
# host that thinks it has ipv6 connectivity, but really doesn't. But in
# principle this could happen for any kind of multi-home situation (e.g. the
# route to one mirror is down but another is up).
#
# The naive algorithm (e.g. the stdlib's socket.create_connection) would be to
# pick one of the IP addresses and try to connect; if that fails, try the
# next; etc. The problem with this is that TCP is stubborn, and if the first
# address is a blackhole then it might take a very long time (tens of seconds)
# before that connection attempt fails.
#
# That's where RFC 6555 comes in. It tells us that what we do is:
# - get the list of IPs from getaddrinfo, trusting the order it gives us (with
#   one exception noted in section 5.4)
# - start a connection attempt to the first IP
# - when this fails OR if it's still going after DELAY seconds, then start a
#   connection attempt to the second IP
# - when this fails OR if it's still going after another DELAY seconds, then
#   start a connection attempt to the third IP
# - ... repeat until we run out of IPs.
#
# Our implementation is similarly straightforward: we spawn a chain of tasks,
# where each one (a) waits until the previous connection has failed or DELAY
# seconds have passed, (b) spawns the next task, (c) attempts to connect. As
# soon as any task crashes or succeeds, we cancel all the tasks and return.
#
# Note: this currently doesn't attempt to cache any results, so if you make
# multiple connections to the same host it'll re-run the happy-eyeballs
# algorithm each time. RFC 6555 is pretty confusing about whether this is
# allowed. Section 4 describes an algorithm that attempts ipv4 and ipv6
# simultaneously, and then says "The client MUST cache information regarding
# the outcome of each connection attempt, and it uses that information to
# avoid thrashing the network with subsequent attempts." Then section 4.2 says
# "implementations MUST prefer the first IP address family returned by the
# host's address preference policy, unless implementing a stateful
# algorithm". Here "stateful" means "one that caches information about
# previous attempts". So my reading of this is that IF you're starting ipv4
# and ipv6 at the same time then you MUST cache the result for ~ten minutes,
# but IF you're "preferring" one protocol by trying it first (like we are),
# then you don't need to cache.
#
# Caching is quite tricky: to get it right you need to do things like detect
# when the network interfaces are reconfigured, and if you get it wrong then
# connection attempts basically just don't work. So we don't even try.

# "Firefox and Chrome use 300 ms"
# https://tools.ietf.org/html/rfc6555#section-6
# Though
#   https://www.researchgate.net/profile/Vaibhav_Bajpai3/publication/304568993_Measuring_the_Effects_of_Happy_Eyeballs/links/5773848e08ae6f328f6c284c/Measuring-the-Effects-of-Happy-Eyeballs.pdf
# claims that Firefox actually uses 0 ms, unless an about:config option is
# toggled and then it uses 250 ms.
DEFAULT_DELAY = 0.250

# How should we call getaddrinfo? In particular, should we use AI_ADDRCONFIG?
#
# The idea of AI_ADDRCONFIG is that it only returns addresses that might
# work. E.g., if getaddrinfo knows that you don't have any IPv6 connectivity,
# then it doesn't return any IPv6 addresses. And this is kinda nice, because
# it means maybe you can skip sending AAAA requests entirely. But in practice,
# it doesn't really work right.
#
# - on Linux/glibc, empirically, the default is to return all addresses, and
# with AI_ADDRCONFIG then it only returns IPv6 addresses if there is at least
# one non-loopback IPv6 address configured... but this can be a link-local
# address, so in practice I guess this is basically always configured if IPv6
# is enabled at all. OTOH if you pass in "::1" as the target address with
# AI_ADDRCONFIG and there's no *external* IPv6 address configured, you get an
# error. So AI_ADDRCONFIG mostly doesn't do anything, even when you would want
# it to, and when it does do something it might break things that would have
# worked.
#
# - on Windows 10, empirically, if no IPv6 address is configured then by
# default they are also suppressed from getaddrinfo (flags=0 and
# flags=AI_ADDRCONFIG seem to do the same thing). If you pass AI_ALL, then you
# get the full list.
# ...except for localhost! getaddrinfo("localhost", "80") gives me ::1, even
# though there's no ipv6 and other queries only return ipv4.
# If you pass in and IPv6 IP address as the target address, then that's always
# returned OK, even with AI_ADDRCONFIG set and no IPv6 configured.
#
# But I guess other versions of windows messed this up, judging from these bug
# reports:
# https://bugs.chromium.org/p/chromium/issues/detail?id=5234
# https://bugs.chromium.org/p/chromium/issues/detail?id=32522#c50
#
# So basically the options are either to use AI_ADDRCONFIG and then add some
# complicated special cases to work around its brokenness, or else don't use
# AI_ADDRCONFIG and accept that sometimes on legacy/misconfigured networks
# we'll waste 300 ms trying to connect to a blackholed destination.
#
# Twisted and Tornado always uses default flags. I think we'll do the same.


@contextmanager
def close_all() -> Generator[set[SocketType], None, None]:
    sockets_to_close: set[SocketType] = set()
    try:
        yield sockets_to_close
    finally:
        errs = []
        for sock in sockets_to_close:
            try:
                sock.close()
            except BaseException as exc:
                errs.append(exc)
        if len(errs) == 1:
            raise errs[0]
        elif errs:
            raise BaseExceptionGroup("", errs)


def reorder_for_rfc_6555_section_5_4(  # type: ignore[explicit-any]
    targets: MutableSequence[tuple[AddressFamily, SocketKind, int, str, Any]],
) -> None:
    # RFC 6555 section 5.4 says that if getaddrinfo returns multiple address
    # families (e.g. IPv4 and IPv6), then you should make sure that your first
    # and second attempts use different families:
    #
    #    https://tools.ietf.org/html/rfc6555#section-5.4
    #
    # This function post-processes the results from getaddrinfo, in-place, to
    # satisfy this requirement.
    for i in range(1, len(targets)):
        if targets[i][0] != targets[0][0]:
            # Found the first entry with a different address family; move it
            # so that it becomes the second item on the list.
            if i != 1:
                targets.insert(1, targets.pop(i))
            break


def format_host_port(host: str | bytes, port: int | str) -> str:
    host = host.decode("ascii") if isinstance(host, bytes) else host
    if ":" in host:
        return f"[{host}]:{port}"
    else:
        return f"{host}:{port}"


# Twisted's HostnameEndpoint has a good set of configurables:
#   https://twistedmatrix.com/documents/current/api/twisted.internet.endpoints.HostnameEndpoint.html
#
# - per-connection timeout
#   this doesn't seem useful -- we let you set a timeout on the whole thing
#   using Trio's normal mechanisms, and that seems like enough
# - delay between attempts
# - bind address (but not port!)
#   they *don't* support multiple address bindings, like giving the ipv4 and
#   ipv6 addresses of the host.
#   I think maybe our semantics should be: we accept a list of bind addresses,
#   and we bind to the first one that is compatible with the
#   connection attempt we want to make, and if none are compatible then we
#   don't try to connect to that target.
#
# XX TODO: implement bind address support
#
# Actually, the best option is probably to be explicit: {AF_INET: "...",
#   AF_INET6: "..."}
# this might be simpler after
async def open_tcp_stream(
    host: str | bytes,
    port: int,
    *,
    happy_eyeballs_delay: float | None = DEFAULT_DELAY,
    local_address: str | None = None,
) -> trio.SocketStream:
    """Connect to the given host and port over TCP.

    If the given ``host`` has multiple IP addresses associated with it, then
    we have a problem: which one do we use?

    One approach would be to attempt to connect to the first one, and then if
    that fails, attempt to connect to the second one ... until we've tried all
    of them. But the problem with this is that if the first IP address is
    unreachable (for example, because it's an IPv6 address and our network
    discards IPv6 packets), then we might end up waiting tens of seconds for
    the first connection attempt to timeout before we try the second address.

    Another approach would be to attempt to connect to all of the addresses at
    the same time, in parallel, and then use whichever connection succeeds
    first, abandoning the others. This would be fast, but create a lot of
    unnecessary load on the network and the remote server.

    This function strikes a balance between these two extremes: it works its
    way through the available addresses one at a time, like the first
    approach; but, if ``happy_eyeballs_delay`` seconds have passed and it's
    still waiting for an attempt to succeed or fail, then it gets impatient
    and starts the next connection attempt in parallel. As soon as any one
    connection attempt succeeds, all the other attempts are cancelled. This
    avoids unnecessary load because most connections will succeed after just
    one or two attempts, but if one of the addresses is unreachable then it
    doesn't slow us down too much.

    This is known as a "happy eyeballs" algorithm, and our particular variant
    is modelled after how Chrome connects to webservers; see `RFC 6555
    <https://tools.ietf.org/html/rfc6555>`__ for more details.

    Args:
      host (str or bytes): The host to connect to. Can be an IPv4 address,
          IPv6 address, or a hostname.

      port (int): The port to connect to.

      happy_eyeballs_delay (float or None): How many seconds to wait for each
          connection attempt to succeed or fail before getting impatient and
          starting another one in parallel. Set to `None` if you want
          to limit to only one connection attempt at a time (like
          :func:`socket.create_connection`). Default: 0.25 (250 ms).

      local_address (None or str): The local IP address or hostname to use as
          the source for outgoing connections. If ``None``, we let the OS pick
          the source IP.

          This is useful in some exotic networking configurations where your
          host has multiple IP addresses, and you want to force the use of a
          specific one.

          Note that if you pass an IPv4 ``local_address``, then you won't be
          able to connect to IPv6 hosts, and vice-versa. If you want to take
          advantage of this to force the use of IPv4 or IPv6 without
          specifying an exact source address, you can use the IPv4 wildcard
          address ``local_address="0.0.0.0"``, or the IPv6 wildcard address
          ``local_address="::"``.

    Returns:
      SocketStream: a :class:`~trio.abc.Stream` connected to the given server.

    Raises:
      OSError: if the connection fails.

    See also:
      open_ssl_over_tcp_stream

    """

    # To keep our public API surface smaller, rule out some cases that
    # getaddrinfo will accept in some circumstances, but that act weird or
    # have non-portable behavior or are just plain not useful.
    if not isinstance(host, (str, bytes)):
        raise ValueError(f"host must be str or bytes, not {host!r}")
    if not isinstance(port, int):
        raise TypeError(f"port must be int, not {port!r}")

    if happy_eyeballs_delay is None:
        happy_eyeballs_delay = DEFAULT_DELAY

    targets = await getaddrinfo(host, port, type=SOCK_STREAM)

    # I don't think this can actually happen -- if there are no results,
    # getaddrinfo should have raised OSError instead of returning an empty
    # list. But let's be paranoid and handle it anyway:
    if not targets:
        msg = f"no results found for hostname lookup: {format_host_port(host, port)}"
        raise OSError(msg)

    reorder_for_rfc_6555_section_5_4(targets)

    # This list records all the connection failures that we ignored.
    oserrors: list[OSError] = []

    # Keeps track of the socket that we're going to complete with,
    # need to make sure this isn't automatically closed
    winning_socket: SocketType | None = None

    # Try connecting to the specified address. Possible outcomes:
    # - success: record connected socket in winning_socket and cancel
    #   concurrent attempts
    # - failure: record exception in oserrors, set attempt_failed allowing
    #   the next connection attempt to start early
    # code needs to ensure sockets can be closed appropriately in the
    # face of crash or cancellation
    async def attempt_connect(
        socket_args: tuple[AddressFamily, SocketKind, int],
        sockaddr: AddressFormat,
        attempt_failed: trio.Event,
    ) -> None:
        nonlocal winning_socket

        try:
            sock = socket(*socket_args)
            open_sockets.add(sock)

            if local_address is not None:
                # TCP connections are identified by a 4-tuple:
                #
                #   (local IP, local port, remote IP, remote port)
                #
                # So if a single local IP wants to make multiple connections
                # to the same (remote IP, remote port) pair, then those
                # connections have to use different local ports, or else TCP
                # won't be able to tell them apart. OTOH, if you have multiple
                # connections to different remote IP/ports, then those
                # connections can share a local port.
                #
                # Normally, when you call bind(), the kernel will immediately
                # assign a specific local port to your socket. At this point
                # the kernel doesn't know which (remote IP, remote port)
                # you're going to use, so it has to pick a local port that
                # *no* other connection is using. That's the only way to
                # guarantee that this local port will be usable later when we
                # call connect(). (Alternatively, you can set SO_REUSEADDR to
                # allow multiple nascent connections to share the same port,
                # but then connect() might fail with EADDRNOTAVAIL if we get
                # unlucky and our TCP 4-tuple ends up colliding with another
                # unrelated connection.)
                #
                # So calling bind() before connect() works, but it disables
                # sharing of local ports. This is inefficient: it makes you
                # more likely to run out of local ports.
                #
                # But on some versions of Linux, we can re-enable sharing of
                # local ports by setting a special flag. This flag tells
                # bind() to only bind the IP, and not the port. That way,
                # connect() is allowed to pick the the port, and it can do a
                # better job of it because it knows the remote IP/port.
                with suppress(OSError, AttributeError):
                    sock.setsockopt(
                        trio.socket.IPPROTO_IP,
                        trio.socket.IP_BIND_ADDRESS_NO_PORT,
                        1,
                    )
                try:
                    await sock.bind((local_address, 0))
                except OSError:
                    raise OSError(
                        f"local_address={local_address!r} is incompatible "
                        f"with remote address {sockaddr!r}",
                    ) from None

            await sock.connect(sockaddr)

            # Success! Save the winning socket and cancel all outstanding
            # connection attempts.
            winning_socket = sock
            nursery.cancel_scope.cancel(reason="successfully found a socket")
        except OSError as exc:
            # This connection attempt failed, but the next one might
            # succeed. Save the error for later so we can report it if
            # everything fails, and tell the next attempt that it should go
            # ahead (if it hasn't already).
            oserrors.append(exc)
            attempt_failed.set()

    with close_all() as open_sockets:
        # nursery spawns a task for each connection attempt, will be
        # cancelled by the task that gets a successful connection
        async with trio.open_nursery() as nursery:
            for address_family, socket_type, proto, _, addr in targets:
                # create an event to indicate connection failure,
                # allowing the next target to be tried early
                attempt_failed = trio.Event()

                nursery.start_soon(
                    attempt_connect,
                    (address_family, socket_type, proto),
                    addr,
                    attempt_failed,
                )

                # give this attempt at most this time before moving on
                with trio.move_on_after(happy_eyeballs_delay):
                    await attempt_failed.wait()

        # nothing succeeded
        if winning_socket is None:
            assert len(oserrors) == len(targets)
            msg = f"all attempts to connect to {format_host_port(host, port)} failed"
            raise OSError(msg) from ExceptionGroup(msg, oserrors)
        else:
            stream = trio.SocketStream(winning_socket)
            open_sockets.remove(winning_socket)
            return stream
