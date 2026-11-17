from __future__ import annotations

import os
import ssl
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates

from .config import Settings
from .errors import ConfigurationError


def build_ssl_context(settings: Settings) -> ssl.SSLContext:
    try:
        key, cert, chain = load_key_and_certificates(
            settings.p12_path.read_bytes(), settings.p12_password.encode()
        )
        if key is None or cert is None:
            raise ValueError("missing certificate or private key")
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        # User-provided trust anchor; keep hostname, expiry and signature validation enabled.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if settings.ca_file:
            context.load_verify_locations(cafile=str(settings.ca_file))
        else:
            context.load_verify_locations(cadata=cert_pem.decode())
            context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        # OpenSSL needs filesystem paths for load_cert_chain; remove these immediately after loading.
        with tempfile.TemporaryDirectory(prefix="ota-mtls-") as directory:
            cert_path, key_path = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            for path, content in (
                (
                    cert_path,
                    cert_pem + b"".join(c.public_bytes(serialization.Encoding.PEM) for c in (chain or [])),
                ),
                (
                    key_path,
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    ),
                ),
            ):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
            context.load_cert_chain(str(cert_path), str(key_path))
        return context
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise ConfigurationError("无法读取 P12 或建立 TLS 信任，请检查证书、证书密码及 CA 配置。") from exc
