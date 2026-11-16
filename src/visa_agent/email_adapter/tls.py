"""TLS trust configuration shared by the SMTP and IMAP clients."""

import os
import ssl


def mail_tls_context() -> ssl.SSLContext:
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())
