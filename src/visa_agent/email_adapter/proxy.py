"""Opt-in SOCKS5 connections for the standard-library mail clients."""

from __future__ import annotations

import imaplib
import smtplib
import socket
import ssl

from python_socks.sync import Proxy


def _connect(proxy_url: str, host: str, port: int, timeout: float | None) -> socket.socket:
    if not proxy_url.startswith("socks5://"):
        raise ValueError("EMAIL_PROXY_URL must be a socks5:// URL")
    proxy = Proxy.from_url(proxy_url, rdns=True)
    connection = proxy.connect(dest_host=host, dest_port=port, timeout=timeout)
    connection.settimeout(timeout)
    return connection


class SocksSMTP(smtplib.SMTP):
    def __init__(self, host: str, port: int, *, proxy_url: str, timeout: float):
        self.proxy_url = proxy_url
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return _connect(self.proxy_url, host, port, timeout)


class SocksIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(
        self, host: str, port: int, *, proxy_url: str, timeout: float,
        ssl_context: ssl.SSLContext,
    ):
        self.proxy_url = proxy_url
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout: float) -> ssl.SSLSocket:
        connection = _connect(self.proxy_url, self.host, self.port, timeout)
        try:
            return self.ssl_context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise
