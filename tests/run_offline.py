"""Run local unittest suites without external network or database access.

The guard permits only loopback socket connections because Windows asyncio
creates its internal wakeup socket with a local socketpair. It denies every
other address and blocks psycopg2 connection creation.
"""
import argparse
import os
from pathlib import Path
import socket
import sys
import unittest


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_socket_connect = socket.socket.connect
_socket_connect_ex = socket.socket.connect_ex
_create_connection = socket.create_connection
_getaddrinfo = socket.getaddrinfo


def _loopback(address):
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    return isinstance(host, str) and host.lower() in _LOOPBACK_HOSTS


def _denied(*_args, **_kwargs):
    raise RuntimeError(
        "Offline regression guard: external network and real DB access are disabled"
    )


def _guarded_connect(sock, address):
    if _loopback(address):
        return _socket_connect(sock, address)
    return _denied()


def _guarded_connect_ex(sock, address):
    if _loopback(address):
        return _socket_connect_ex(sock, address)
    return _denied()


def _guarded_create_connection(address, *args, **kwargs):
    if _loopback(address):
        return _create_connection(address, *args, **kwargs)
    return _denied()


def _guarded_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    if isinstance(host, str) and host.lower() in _LOOPBACK_HOSTS:
        return _getaddrinfo(host, *args, **kwargs)
    return _denied()


def _install_guards():
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.create_connection = _guarded_create_connection
    socket.getaddrinfo = _guarded_getaddrinfo
    try:
        import psycopg2
        psycopg2.connect = _denied
        psycopg2._connect = _denied
    except ImportError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--pattern")
    args = parser.parse_args()
    root = Path(args.repo).resolve()
    os.chdir(root)
    sys.path[:0] = [str(root / "gojo_backend"), str(root / "tests"), str(root)]
    _install_guards()
    patterns = [args.pattern] if args.pattern else (
        ["test_*.py"] if args.full else [
            "test_cognitive*.py",
            "test_structured_output*.py",
            "test_model_failures.py",
            "test_memory_structured_output.py",
        ]
    )
    suite = unittest.TestSuite()
    for pattern in patterns:
        suite.addTests(unittest.defaultTestLoader.discover(str(root / "tests"), pattern=pattern))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(
        "offline_guard=external-network-and-real-psycopg2-disabled "
        f"tests_run={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
